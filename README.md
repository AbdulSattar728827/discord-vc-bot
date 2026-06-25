# 🎙️ Discord VC Leaderboard Bot

A Discord bot that **tracks how long every member spends in Voice Channels** and
posts a live, auto-updating leaderboard in a dedicated channel.

---

## ✨ Features

| Feature | Details |
|---|---|
| **VC Time Tracking** | Records every join and leave event for every member |
| **Live Leaderboard** | Auto-refreshes every 5 minutes in `#vc-leaderboard` |
| **Top 3 Highlighted** | 🥇🥈🥉 get their own gold/silver/bronze embed cards |
| **All Members Listed** | Everyone appears, even after just 1 minute of VC time |
| **Session Logs** | Saved to `data/vc_logs.json` with join time, leave time, duration, rank |
| **Bot Logs** | Human-readable log file at `data/bot.log` |
| **Slash Command** | `/refresh_leaderboard` for admins to force a refresh |
| **24/7 Ready** | Comes with a `systemd` service file for Linux VPS hosting |

---

## 📁 Project Structure

```
discord-vc-bot/
├── bot.py                  ← Entry point (run this)
├── requirements.txt        ← Python dependencies
├── .env.example            ← Copy this to .env and fill in your token
├── .gitignore
├── vc-bot.service          ← Linux systemd service (for 24/7 hosting)
├── cogs/
│   ├── __init__.py
│   ├── tracker.py          ← Listens to voice events, calculates time
│   └── leaderboard.py      ← Manages the leaderboard channel & embeds
└── data/                   ← Auto-created at runtime
    ├── vc_stats.json       ← Cumulative VC seconds per user
    ├── vc_logs.json        ← Per-session logs with rank
    └── bot.log             ← Bot activity log
```

---

## 🚀 Step-by-Step Setup (Beginner-Friendly)

### STEP 1 — Create Your Discord Bot Application

1. Go to **https://discord.com/developers/applications**
2. Click **"New Application"** → give it a name (e.g. `VC Tracker`) → **Create**
3. On the left sidebar, click **"Bot"**
4. Click **"Reset Token"** → confirm → **copy the token** (save it somewhere safe — you won't see it again)
5. Scroll down to **"Privileged Gateway Intents"** and turn ON:
   - ✅ **Server Members Intent**
   - ✅ **Voice State Intent** (may already be on)
   - ✅ **Message Content Intent** (just in case)
6. Click **Save Changes**

### STEP 2 — Invite the Bot to Your Server

1. In the Developer Portal, go to **"OAuth2"** → **"URL Generator"**
2. Under **Scopes**, check:
   - ✅ `bot`
   - ✅ `applications.commands`
3. Under **Bot Permissions**, check:
   - ✅ `View Channels`
   - ✅ `Send Messages`
   - ✅ `Manage Messages`
   - ✅ `Embed Links`
   - ✅ `Manage Channels`  ← needed to create `#vc-leaderboard` automatically
4. Copy the generated URL at the bottom → open it in your browser
5. Select your server → **Authorize** → pass the CAPTCHA

### STEP 3 — Set Up the Bot on Your Computer (or VPS)

You need **Python 3.11 or newer**. Check with:
```bash
python3 --version
```

If you don't have it, download it from **https://www.python.org/downloads/**

#### Clone / Download the project
```bash
# If you have git:
git clone https://github.com/YOUR_USERNAME/discord-vc-bot.git
cd discord-vc-bot

# Or just download the ZIP and extract it, then open a terminal in that folder
```

#### Create a virtual environment (keeps dependencies isolated)
```bash
python3 -m venv venv

# Activate it:
# On Windows:
venv\Scripts\activate
# On Mac/Linux:
source venv/bin/activate
```

#### Install dependencies
```bash
pip install -r requirements.txt
```

#### Set up your token
```bash
# Copy the example file:
cp .env.example .env

# Open .env in any text editor and replace YOUR_BOT_TOKEN_HERE
# with the token you copied in Step 1
```

Your `.env` file should look like:
```
DISCORD_TOKEN=MTExNjY2NzM0...your_actual_token_here
```

### STEP 4 — Run the Bot

```bash
python bot.py
```

You should see output like:
```
2024-01-15 12:00:00 [INFO] Bot logged in as VC Tracker#1234 (ID: 123456789)
2024-01-15 12:00:00 [INFO] Connected to 1 guild(s).
2024-01-15 12:00:00 [INFO] All cogs loaded successfully.
2024-01-15 12:00:00 [INFO] Synced 1 slash command(s).
2024-01-15 12:00:01 [INFO] [YOUR_GUILD_ID] Created #vc-leaderboard
```

✅ The bot is running! It will automatically create `#vc-leaderboard` in your server.

---

## 🖥️ Running 24/7 on a Linux VPS (Recommended)

A VPS (Virtual Private Server) keeps the bot online even when your PC is off.
Cheap options: **Railway** (free tier), **Hetzner** (~€4/month), **DigitalOcean** (~$4/month).

### Using systemd (the right way on Linux)

1. Upload your project to the server (e.g. using `scp` or `git clone`)
2. Edit `vc-bot.service` — replace `YOUR_USERNAME` with your actual Linux username
3. Copy the service file:
```bash
sudo cp vc-bot.service /etc/systemd/system/vc-bot.service
```
4. Reload systemd and enable the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable vc-bot      # auto-start on reboot
sudo systemctl start vc-bot       # start right now
```
5. Check it's running:
```bash
sudo systemctl status vc-bot
```
6. View live logs:
```bash
sudo journalctl -u vc-bot -f
```

### Using screen (simpler, but not auto-restart on reboot)
```bash
screen -S vcbot
python bot.py
# Press Ctrl+A then D to detach
# Reconnect later with: screen -r vcbot
```

### Free Hosting on Railway
1. Go to **https://railway.app** → sign up with GitHub
2. Click **New Project** → **Deploy from GitHub repo**
3. Select your bot repo
4. Under **Variables**, add `DISCORD_TOKEN` = your token
5. Railway will keep it running 24/7 for free (within limits)

---

## 📋 Log File Examples

### `data/vc_logs.json`
```json
[
  {
    "user_id": "123456789",
    "username": "PlayerOne",
    "channel": "General",
    "joined_at": "2024-01-15T10:30:00+00:00",
    "left_at":   "2024-01-15T11:15:30+00:00",
    "duration_s": 2730.0,
    "duration":   "45m 30s",
    "rank":       "1st",
    "total_time": "3h 12m 45s"
  }
]
```

### `data/bot.log`
```
2024-01-15 10:30:00 [INFO] [98765] PlayerOne joined #General at 2024-01-15 10:30:00 UTC
2024-01-15 11:15:30 [INFO] [98765] PlayerOne left #General | session 45m 30s | total 3h 12m 45s | rank 1st
```

---

## ⚙️ Customisation

Open `cogs/leaderboard.py` and change these constants at the top:

```python
LEADERBOARD_CHANNEL_NAME = "vc-leaderboard"   # rename the channel
REFRESH_INTERVAL_MINUTES = 5                   # how often it auto-updates
```

---

## 🛠️ Slash Commands

| Command | Who can use | What it does |
|---|---|---|
| `/refresh_leaderboard` | Admins only | Force-updates the leaderboard immediately |

---

## ❓ Troubleshooting

| Problem | Solution |
|---|---|
| `DISCORD_TOKEN not found` | Make sure you created `.env` (not `.env.example`) and the token is correct |
| Bot doesn't create `#vc-leaderboard` | Make sure you gave the bot **Manage Channels** permission when inviting |
| Bot is online but leaderboard is empty | Members need to actually join a VC after the bot starts |
| `Missing permission` errors in logs | Re-invite the bot with all permissions listed in Step 2 |
| Bot goes offline | Use systemd or Railway for 24/7 uptime (see above) |

---

## 📜 License

MIT — use freely, modify as you like.
