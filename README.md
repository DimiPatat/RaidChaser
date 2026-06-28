# Raid Signup Chaser - Setup Guide

A Discord bot that watches your Raid-Helper events and automatically chases
anyone with the Raider/Trial roles who hasn't signed up. Escalates from a
gentle DM to a firmer DM to a public ping plus an officer summary.

Built for raids on **Monday and Wednesday 20:00**, but it works for any
schedule - it reads each event's real start time from Raid-Helper.

---

## What it does

For every upcoming raid in your signup channel, it checks who holds the
Raider/Trial roles, compares against who's responded on the Raid-Helper post,
and chases the gap on this schedule (all editable):

| Stage | When        | Action                                      |
|-------|-------------|---------------------------------------------|
| 1     | 24h before  | Soft DM nudge                               |
| 2     | 6h before   | Firmer DM                                   |
| 3     | 2h before   | Public ping in chase channel + officer list |

"Responded" means signed up in any way - Accepted, Tentative, Late, Bench, or
Absence. It only chases people who gave **no response at all**.

---

## Step 1: Create the bot in Discord (5 min)

1. Go to https://discord.com/developers/applications
2. **New Application**, name it (e.g. "Raid Chaser"), create.
3. Left menu > **Bot** > **Add Bot**.
4. Under **Privileged Gateway Intents**, turn ON **Server Members Intent**.
   (The bot needs this to see who holds which roles. This is essential.)
5. Click **Reset Token**, copy the token. This is your `DISCORD_TOKEN`.
   Keep it secret - anyone with it controls the bot.

## Step 2: Invite the bot to your server

1. Left menu > **OAuth2** > **URL Generator**.
2. Scopes: tick **bot** AND **applications.commands**.
3. Bot Permissions: tick **Send Messages**, **Embed Links**,
   **Mention Everyone** (needed to ping roles/members), **View Channels**.
4. Copy the generated URL at the bottom, open it, pick your server, authorise.

## Step 3: Collect your IDs

Turn on Developer Mode first: Discord **Settings > Advanced > Developer Mode** ON.

Then right-click to "Copy ID" for each:
- **Server ID** (right-click server icon) -> `GUILD_ID`
- **Signup channel** (where Raid-Helper posts events) -> `SIGNUP_CHANNEL_ID`
- **Chase channel** (where public pings go) -> `CHASE_CHANNEL_ID`
- **Officer channel** (where the missing-list goes) -> `OFFICER_CHANNEL_ID`
- **Raider and Trial roles** (optional): Server Settings > Roles, right-click
  each role > Copy ID. Putting them in `RAIDER_ROLE_IDS` seeds the chase list
  on the very first run. After that you manage roles live with `/roles` commands.

---

## Step 4: Host it free on Render + UptimeRobot

This is a two-part setup: **Render** runs the bot for free, and **UptimeRobot**
pings it every 10 minutes to stop Render spinning it down.

### Part A - Deploy on Render

1. Put these files in a GitHub repo: `bot.py`, `requirements.txt`, `Procfile`.
2. Go to https://render.com and sign in with GitHub (free account).
3. Click **New > Web Service**.
4. Connect your GitHub repo and pick it from the list.
5. Render will auto-detect settings. Make sure:
   - **Environment**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python bot.py`
   - **Instance Type**: Free
6. Click **Advanced > Add Environment Variable** and add each variable from
   `.env.example` (DISCORD_TOKEN, GUILD_ID, etc.) with your real values.
   Do NOT upload the `.env` file itself.
7. Click **Create Web Service**. Render will deploy and show logs.
   You should see "Logged in as Raid Chaser..." within a minute or two.
8. Copy your service URL from the top of the page - it looks like
   `https://your-bot-name.onrender.com`. You'll need this for UptimeRobot.

### Part B - Keep it alive with UptimeRobot (free)

Render's free tier spins down services after 15 minutes of no web traffic.
UptimeRobot pings your bot every 10 minutes so it never goes to sleep.

1. Go to https://uptimerobot.com and create a free account.
2. Click **Add New Monitor**.
3. Set:
   - **Monitor Type**: HTTP(s)
   - **Friendly Name**: Raid Chaser (or anything you like)
   - **URL**: your Render URL from step 8 above (e.g. `https://your-bot-name.onrender.com`)
   - **Monitoring Interval**: 5 minutes
4. Click **Create Monitor**. Done.

UptimeRobot will now ping your bot every 5 minutes, which keeps Render from
spinning it down. Your bot runs 24/7 at no cost.

### Persistent role storage on Render

By default `roles.json` is lost on every redeploy (Render's free disk is
ephemeral). This means `/roles add` changes won't survive a redeploy. To fix it:

1. In your Render service, go to **Settings > Disks**.
2. Add a disk, mount path `/data`, size 1 GB (free tier allows this).
3. Add an environment variable: `ROLES_FILE=/data/roles.json`.

After that, role changes survive forever. If you skip this, it still works -
you'll just need to re-add roles after each redeploy.

---

## Managing which roles get chased (live, no redeploy)

Once the bot is running, any officer (anyone with Manage Server) can change the
chase list straight from Discord:

- `/roles add @Raider`   - start chasing people with that role
- `/roles remove @Trial` - stop chasing that role
- `/roles list`          - show what's currently being chased

Changes take effect immediately and survive restarts (stored in `roles.json`).
The replies are only visible to you (ephemeral), so they don't clutter the channel.

---

## Step 5: Tweak the timings

Open `bot.py`, find the `STAGES` list near the top. Each stage has:
- `hours_before` - when it fires relative to raid start
- `method` - "dm" or "ping"
- `message` - the wording (uses {name}, {event}, {time})

Change numbers or text, push to GitHub and Render redeploys automatically.
The loop checks every 15 minutes, so timings are accurate to within 15 min.

---

## Notes and gotchas

- **DMs can be blocked.** If a raider has server DMs off, stages 1-2 silently
  fail for them, but stage 3 (public ping) always reaches them. This is why
  the escalation ends in a public channel.
- **Restart resets the "already chased" memory.** Worst case after a restart,
  someone gets one duplicate nudge. Harmless.
- **The bot only chases events in your signup channel.** Other Raid-Helper
  events elsewhere are ignored.
- **If Raid-Helper changes their API**, the field names in bot.py
  (`startTime`, `signUps`, `userId`) may need a tweak. They're all in clearly
  marked helper functions near the top if so.
- **Render cold starts.** On the rare occasion Render does spin down (e.g. if
  UptimeRobot had a hiccup), the first ping wakes it back up within ~30 seconds.
  You won't miss a chase window since the loop runs every 15 minutes.
