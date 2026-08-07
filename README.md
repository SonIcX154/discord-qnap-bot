# Discord QNAP Bot

A Discord bot built for **QNAP Container Station** (Docker). It combines persistent voice presence, birthdays, an economy/minigame system, full server message backup & restore, and an optional Twitch ↔ Discord chat mirror.

## Features

| Area | What it does |
|------|----------------|
| **Voice stayer** | Stays connected to a configured voice channel 24/7 with auto-reconnect |
| **Birthdays** | Per-guild storage, slash commands, daily announcements; leavers kept but hidden (`in_guild`) |
| **Economy** | Coins, daily, give/beg, leaderboard, chat/voice earnings |
| **Minigames** | `/coinflip`, `/roulette`, `/slots` (owner-only buttons) |
| **Backup** | Live message logging, attachments, structure snapshots, structure/message restore, soft-delete retention, force-sync |
| **Twitch mirror** | Twitch → Discord webhooks, Discord → Twitch, bidirectional deletes, robotty catch-up |
| **BadgeBase** | Notify on newly claimable Twitch badges |
| **Status** | Single admin overview: `/bot-status` |

## Repo layout

```text
bot.py                 # launcher, intents, ENABLED_COGS filter, slash sync, global errors
cogs/
  voice_stayer.py      # stay in a voice channel
  presence.py          # rich presence (next birthday)
  birthdays.py
  economy.py
  roulette.py / slots.py
  backup.py            # core backup cog
  backup_admin.py      # optional admin helpers
  twitch_mirror.py     # Twitch ↔ Discord mirror + catch-up
  badgebase.py
  status.py            # /bot-status unified overview
utils/                 # shared helpers, stores, mixins
data/                  # runtime DBs (gitignored)
scripts/               # rebuild helpers for QNAP
```

## Quick start

```bash
cp .env.example .env
# fill DISCORD_TOKEN and optional feature vars
pip install -r requirements.txt
python bot.py
```

### Docker / Container Station

```bash
docker compose up -d --build
```

- **Restart:** `unless-stopped`
- **Volume:** `./data:/app/data` — keep this mapped so DBs and attachments survive rebuilds
- **Healthcheck:** process liveness on PID 1 (see `docker-compose.yml`)
- **Timezone:** `TZ=Europe/Berlin` (daily reset, birthday announce, restore labels)

### Slim instance (selected cogs only)

By default every `cogs/*.py` loads. To run e.g. economy + birthdays + Twitch without backup:

```env
ENABLED_COGS=economy,roulette,slots,birthdays,twitch_mirror,status,presence
```

Names = filename without `.py`. Empty / unset = load all.

Use a **separate Discord application token** if you run a second container (same token in two gateways conflicts).

## Slash commands (overview)

### Core / admin

- `/bot-status` — latency, uptime, loaded cogs, voice, Twitch, backup, economy, BadgeBase  
  Missing cogs (via `ENABLED_COGS`) show as `not loaded`.

### Economy

Passive earnings: chat coins (hourly cap), voice coins when ≥2 active users in a VC.

### Minigames

- `/roulette <bet>` — color/parity/range bets, adjustable stake  
- `/slots <bet>` — animated slots + replay  

Only the user who ran the command can use the buttons. Others get an ephemeral:  
*Um zu spielen führe den Command bitte selber aus.*

### Birthdays

- `/geburtstag-setzen`, `/geburtstag-entfernen`, `/geburtstags-liste`, `/geburtstag-heute`  
- Members who leave keep their entry with `in_guild: false` (no list / announce / presence). Rejoin restores them.

### Backup (administrator)

- `/bot-status` — unified overview  
- `/backup-backfill` — force-sync history + mark missing IDs deleted  
- `/backup-snapshot`, `/backup-snapshots`, `/backup-restore`  
- `/backup-restore-messages` — webhook replay with original name/avatar  

### Twitch mirror

Requires `TWITCH_TOKEN`, `TWITCH_CHANNEL`, `TWITCH_DISCORD_CHANNEL_ID` (and Helix scopes for send/delete).

- IRC idle watchdog reconnects on blackholed sockets (e.g. router gateway change)
- robotty catch-up on reconnect + periodic (defaults: `TWITCH_CATCHUP_LIMIT=50`, `TWITCH_CATCHUP_INTERVAL=300`)
- Twitch `/clear` only wipes Discord mirror messages from the last `TWITCH_CLEAR_WINDOW_SECONDS` (default 600)

### BadgeBase

- Notifications for newly claimable badges (channel via `/badgebase-channel`)
- `/badgebase-claimable` — missing claimable badges for a Twitch login

## Environment

See `.env.example`. Highlights:

| Variable | Role |
|----------|------|
| `DISCORD_TOKEN` | required |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `ENABLED_COGS` | optional comma-list of cog modules |
| `BOT_DEV_ID` / `BOT_DEV_IDS` | owner bypass for admin cmds without Manage Guild |
| `VOICE_CHANNEL_ID` | voice stayer target |
| Twitch / BadgeBase / backup paths | see `.env.example` |

## Troubleshooting

| Symptom | Check |
|---------|--------|
| Voice reconnect loop | Valid `VOICE_CHANNEL_ID`, bot can join |
| Twitch not mirroring | Token, client id, channel name, Discord channel ID; bot mod for deletes |
| Mirror dies after router blip | Idle watchdog + catch-up; restart container if needed |
| Birthday shows raw user id | Should be fixed via `in_guild`; restart once to reconcile |
| Backup restore hierarchy odd | Bot role must be highest manageable; use `clear_first` carefully |
| Unexpected command errors | Bot logs show full traceback; users get an ephemeral message |
| Container unhealthy | Check logs; healthcheck only verifies process alive |

## Extending

Drop a new `cogs/*.py` with a `setup(bot)` and it loads automatically (unless filtered by `ENABLED_COGS`). Shared UI patterns: `BetAdjustableMixin`, `ReplayMixin`, `OWNER_ONLY_MSG`.

Per-command `@command.error` handlers still override the global tree error handler when present (e.g. cooldowns on roulette/slots).

---

Built for QNAP Container Station — persistent voice, birthdays, economy, disaster-ready backup, and optional Twitch chat bridging.
