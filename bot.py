"""
Raid Signup Chaser
------------------
Watches a Raid-Helper signup channel, finds upcoming events, and chases
anyone holding the raider/trial roles who hasn't signed up yet.

Escalation runs on a schedule relative to each event's start time:
  - Stage 1: soft DM
  - Stage 2: firmer DM
  - Stage 3: public channel ping + officer summary

All timings and IDs are set via environment variables (see .env.example).
Tweak the STAGES list below to change the escalation behaviour.

Raider/trial roles can be managed live in Discord (no redeploy) with:
  /roles add <role>     - start chasing people with this role
  /roles remove <role>  - stop chasing this role
  /roles list           - show the roles currently being chased
These are officer-only (requires Manage Server) and persist to roles.json.
The RAIDER_ROLE_IDS env var seeds the list the very first time only.
"""

import os
import json
import asyncio
import logging
from datetime import datetime, timezone, timedelta

import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import tasks

# ---------------------------------------------------------------------------
# CONFIG - most of this comes from environment variables.
# ---------------------------------------------------------------------------

TOKEN = os.environ["DISCORD_TOKEN"]                 # bot token
GUILD_ID = int(os.environ["GUILD_ID"])              # your server ID
SIGNUP_CHANNEL_ID = int(os.environ["SIGNUP_CHANNEL_ID"])  # channel Raid-Helper posts events to
CHASE_CHANNEL_ID = int(os.environ["CHASE_CHANNEL_ID"])    # channel for public pings (stage 3)
OFFICER_CHANNEL_ID = int(os.environ["OFFICER_CHANNEL_ID"])  # channel for officer summaries

# Comma-separated role IDs to SEED the chase list on very first run only.
# After that, roles are managed live via /roles commands and stored in roles.json.
SEED_ROLE_IDS = {int(x) for x in os.environ.get("RAIDER_ROLE_IDS", "").split(",") if x.strip()}

# Where the live role list is stored so it survives restarts.
ROLES_FILE = os.environ.get("ROLES_FILE", "roles.json")

# How far ahead to look for events (hours). 48 covers Mon/Wed comfortably.
LOOKAHEAD_HOURS = int(os.environ.get("LOOKAHEAD_HOURS", "48"))

# ---------------------------------------------------------------------------
# ESCALATION STAGES
# ---------------------------------------------------------------------------
# hours_before  = how many hours before raid start this stage fires
# method        = "dm" or "ping"
# officer_summary = also post a summary to the officer channel
# message       = the text sent. {name} = member's display name,
#                 {event} = event title, {time} = raid start (Discord timestamp)
#
# Edit these freely. Stages fire in order; each person only gets each stage once.
# Default reflects your Mon/Wed 20:00 raids: T-24h, T-6h, T-2h.
# ---------------------------------------------------------------------------

STAGES = [
    {
        "key": "stage1",
        "hours_before": 24,
        "method": "dm",
        "officer_summary": False,
        "message": (
            "Hey {name}, quick nudge - you're not signed up for **{event}** "
            "starting {time}. If you can make it, drop a sign-up on the event "
            "post when you get a sec. Cheers!"
        ),
    },
    {
        "key": "stage2",
        "hours_before": 6,
        "method": "dm",
        "officer_summary": False,
        "message": (
            "Hey {name}, still no sign-up from you for **{event}** ({time}). "
            "We're firming up the roster soon - please sign up (or hit Absence/Tentative) "
            "so we know where we stand. Ta!"
        ),
    },
    {
        "key": "stage3",
        "hours_before": 2,
        "method": "ping",
        "officer_summary": True,
        "message": (
            "{name} - last call for **{event}** ({time}). Still no sign-up. "
            "Please respond on the event post now so we can sort the roster."
        ),
    },
]

# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("raidchaser")

RAIDHELPER_API = "https://raid-helper.dev/api"

intents = discord.Intents.default()
intents.members = True          # needed to read who holds the raider roles
intents.message_content = False
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ---------------------------------------------------------------------------
# Live raider-role list (persisted to disk, managed via /roles commands)
# ---------------------------------------------------------------------------

def load_roles() -> set[int]:
    """Load the chase-role list from disk. Seed from env on first ever run."""
    try:
        with open(ROLES_FILE, "r") as f:
            return {int(x) for x in json.load(f)}
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        # First run (or unreadable file): seed from env and save.
        save_roles(SEED_ROLE_IDS)
        return set(SEED_ROLE_IDS)


def save_roles(role_ids: set[int]) -> None:
    try:
        with open(ROLES_FILE, "w") as f:
            json.dump(sorted(role_ids), f)
    except Exception as e:
        log.error("Could not save roles file: %s", e)


# Loaded once at startup, kept in memory, written on every change.
RAIDER_ROLE_IDS: set[int] = set()

# Tracks which stages we've already fired, so nobody gets chased twice for the
# same stage of the same event. Key: event_id -> set of stage keys already done.
# In-memory only; resets on restart (safe - worst case one repeat nudge).
_fired: dict[str, set[str]] = {}


# ---------------------------------------------------------------------------
# Raid-Helper API helpers
# ---------------------------------------------------------------------------

async def fetch_server_events(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch upcoming events on the server. No auth needed for read."""
    url = f"{RAIDHELPER_API}/v3/servers/{GUILD_ID}/events"
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                log.warning("Event list fetch returned %s", resp.status)
                return []
            data = await resp.json()
            # API shape: {"postedEvents": [...]} or similar - handle both.
            events = data.get("postedEvents") or data.get("events") or []
            return events
    except Exception as e:
        log.error("Failed to fetch event list: %s", e)
        return []


async def fetch_event_detail(session: aiohttp.ClientSession, event_id: str) -> dict | None:
    """Fetch a single event's full data including signups. No auth needed."""
    url = f"{RAIDHELPER_API}/v2/events/{event_id}"
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                log.warning("Event %s detail fetch returned %s", event_id, resp.status)
                return None
            return await resp.json()
    except Exception as e:
        log.error("Failed to fetch event %s: %s", event_id, e)
        return None


def extract_signed_up_ids(event_detail: dict) -> set[str]:
    """Pull the set of Discord user IDs who have signed up to an event.

    Counts anyone who interacted with the event (Accepted, Tentative, Late,
    Bench, Absence). We only chase people who gave NO response at all.
    """
    ids = set()
    for s in event_detail.get("signUps", []):
        uid = s.get("userId") or s.get("userid")
        if uid:
            ids.add(str(uid))
    return ids


def event_start_dt(event_detail: dict) -> datetime | None:
    """Get the event start time as a timezone-aware UTC datetime."""
    # Raid-Helper returns a unix timestamp in 'startTime' (seconds).
    ts = event_detail.get("startTime") or event_detail.get("starttime")
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (ValueError, TypeError):
        return None


def event_channel_id(event_detail: dict) -> str | None:
    return str(event_detail.get("channelId") or event_detail.get("channelid") or "")


# ---------------------------------------------------------------------------
# Core chase logic
# ---------------------------------------------------------------------------

async def process_event(session, guild, event_summary):
    event_id = str(event_summary.get("id") or event_summary.get("eventId") or "")
    if not event_id:
        return

    detail = await fetch_event_detail(session, event_id)
    if not detail:
        return

    # Only watch events posted to our signup channel.
    if event_channel_id(detail) != str(SIGNUP_CHANNEL_ID):
        return

    start = event_start_dt(detail)
    if not start:
        return

    now = datetime.now(timezone.utc)
    if start < now:
        return  # already started/past

    hours_until = (start - now).total_seconds() / 3600.0
    title = detail.get("title", "the raid")
    discord_ts = f"<t:{int(start.timestamp())}:F>"

    # Which stage should be active right now? Fire the most urgent stage whose
    # window we've entered and haven't yet fired for this event.
    _fired.setdefault(event_id, set())

    signed_up = extract_signed_up_ids(detail)

    # Determine non-responders among raider-role holders.
    missing_members = [
        m for m in guild.members
        if not m.bot
        and any(r.id in RAIDER_ROLE_IDS for r in m.roles)
        and str(m.id) not in signed_up
    ]

    for stage in STAGES:
        if stage["key"] in _fired[event_id]:
            continue
        # Fire when we're inside the window: hours_until <= hours_before,
        # but not so late we've blown past it by more than ~1 hour (loop runs every 15 min).
        if hours_until <= stage["hours_before"]:
            await run_stage(guild, stage, missing_members, title, discord_ts)
            _fired[event_id].add(stage["key"])
            # Only fire one stage per pass so escalation steps stay distinct.
            break


async def run_stage(guild, stage, missing_members, title, discord_ts):
    if not missing_members:
        log.info("Stage %s for '%s': nobody to chase.", stage["key"], title)
        return

    log.info("Stage %s for '%s': chasing %d members.",
             stage["key"], title, len(missing_members))

    if stage["method"] == "dm":
        for m in missing_members:
            text = stage["message"].format(name=m.display_name, event=title, time=discord_ts)
            try:
                await m.send(text)
            except discord.Forbidden:
                log.info("Could not DM %s (DMs closed).", m.display_name)
            except Exception as e:
                log.warning("DM to %s failed: %s", m.display_name, e)
            await asyncio.sleep(1)  # gentle on rate limits

    elif stage["method"] == "ping":
        chase_ch = guild.get_channel(CHASE_CHANNEL_ID)
        if chase_ch:
            # Batch the pings so it's one message, not spam.
            mentions = " ".join(m.mention for m in missing_members)
            body = stage["message"].format(name=mentions, event=title, time=discord_ts)
            try:
                await chase_ch.send(body)
            except Exception as e:
                log.warning("Chase ping failed: %s", e)

    if stage.get("officer_summary"):
        off_ch = guild.get_channel(OFFICER_CHANNEL_ID)
        if off_ch:
            names = "\n".join(f"- {m.display_name}" for m in missing_members)
            summary = (
                f"**Roster check - {title}** ({discord_ts})\n"
                f"{len(missing_members)} still not signed up:\n{names}"
            )
            try:
                await off_ch.send(summary)
            except Exception as e:
                log.warning("Officer summary failed: %s", e)


# ---------------------------------------------------------------------------
# Scheduler - runs every 15 minutes
# ---------------------------------------------------------------------------

@tasks.loop(minutes=15)
async def chase_loop():
    guild = client.get_guild(GUILD_ID)
    if not guild:
        log.warning("Guild %s not found.", GUILD_ID)
        return

    async with aiohttp.ClientSession() as session:
        events = await fetch_server_events(session)
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(hours=LOOKAHEAD_HOURS)

        for ev in events:
            # Light filter on summary timestamp if present, else process anyway.
            ts = ev.get("startTime") or ev.get("starttime")
            if ts:
                try:
                    start = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                    if start < now or start > horizon:
                        continue
                except (ValueError, TypeError):
                    pass
            await process_event(session, guild, ev)

    # Tidy memory: drop fired-records for events well in the past.
    # (Cheap approximation - just cap the dict size.)
    if len(_fired) > 200:
        _fired.clear()


@chase_loop.before_loop
async def before_chase():
    await client.wait_until_ready()


# ---------------------------------------------------------------------------
# Slash commands: /roles add | remove | list  (officer-only)
# ---------------------------------------------------------------------------

roles_group = app_commands.Group(
    name="roles",
    description="Manage which roles the raid chaser nudges.",
    default_permissions=discord.Permissions(manage_guild=True),  # officer-only
)


@roles_group.command(name="add", description="Start chasing members with this role.")
@app_commands.describe(role="The role to start chasing (e.g. Raider, Trial).")
async def roles_add(interaction: discord.Interaction, role: discord.Role):
    if role.id in RAIDER_ROLE_IDS:
        await interaction.response.send_message(
            f"**{role.name}** is already being chased.", ephemeral=True)
        return
    RAIDER_ROLE_IDS.add(role.id)
    save_roles(RAIDER_ROLE_IDS)
    await interaction.response.send_message(
        f"Added **{role.name}**. Members with this role will now be chased.",
        ephemeral=True)


@roles_group.command(name="remove", description="Stop chasing members with this role.")
@app_commands.describe(role="The role to stop chasing.")
async def roles_remove(interaction: discord.Interaction, role: discord.Role):
    if role.id not in RAIDER_ROLE_IDS:
        await interaction.response.send_message(
            f"**{role.name}** isn't on the chase list.", ephemeral=True)
        return
    RAIDER_ROLE_IDS.discard(role.id)
    save_roles(RAIDER_ROLE_IDS)
    await interaction.response.send_message(
        f"Removed **{role.name}**. It will no longer be chased.", ephemeral=True)


@roles_group.command(name="list", description="Show the roles currently being chased.")
async def roles_list(interaction: discord.Interaction):
    if not RAIDER_ROLE_IDS:
        await interaction.response.send_message(
            "No roles are being chased yet. Add one with `/roles add`.",
            ephemeral=True)
        return
    guild = interaction.guild
    lines = []
    for rid in RAIDER_ROLE_IDS:
        r = guild.get_role(rid) if guild else None
        lines.append(f"- {r.name}" if r else f"- (deleted role {rid})")
    await interaction.response.send_message(
        "Currently chasing:\n" + "\n".join(lines), ephemeral=True)


tree.add_command(roles_group)


# ---------------------------------------------------------------------------
# Keepalive web server - required by Render to treat this as a "web service"
# and pinged by UptimeRobot every 10 min to prevent spin-down.
# Listens on PORT env var (Render sets this automatically).
# ---------------------------------------------------------------------------

async def handle_ping(request):
    return web.Response(text="ok")


async def start_keepalive():
    port = int(os.environ.get("PORT", 8080))
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Keepalive server listening on port %s", port)


@client.event
async def on_ready():
    global RAIDER_ROLE_IDS
    RAIDER_ROLE_IDS = load_roles()
    log.info("Logged in as %s (watching guild %s)", client.user, GUILD_ID)
    log.info("Chasing %d role(s): %s", len(RAIDER_ROLE_IDS), sorted(RAIDER_ROLE_IDS))
    # Register slash commands to your guild (instant; global sync can take an hour).
    try:
        guild_obj = discord.Object(id=GUILD_ID)
        tree.copy_global_to(guild=guild_obj)
        await tree.sync(guild=guild_obj)
        log.info("Slash commands synced.")
    except Exception as e:
        log.error("Slash command sync failed: %s", e)
    if not chase_loop.is_running():
        chase_loop.start()
    await start_keepalive()


if __name__ == "__main__":
    client.run(TOKEN)
