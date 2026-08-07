# Discord QNAP Bot

A Discord bot built for **QNAP Container Station** (Docker). It combines persistent voice presence, birthdays, an economy/minigame system, full server message backup & restore, and an optional Twitch ↔ Discord chat mirror.

## Features

| Area | What it does |
|------|----------------|
| **Voice stayer** | Stays connected to a configured voice channel 24/7 with auto-reconnect |
| **Birthdays** | Per-guild birthday storage, slash commands, daily announcements |
| **Economy** | Coins, daily, give/beg, leaderboard, chat/voice earnings |
| **Minigames** | `/coinflip`, `/roulette`, `/slots` (owner-only buttons) |
| **Backup** | Live message logging, attachments, structure snapshots, structure/message restore, soft-delete retention, force-sync |
| **Twitch mirror** | Twitch → Discord webhooks, Discord → Twitch, bidirectional deletes |

## Repo layout

```text
bot.py                 # launcher, intents, cog auto-load, slash sync, global errors
cogs/
  presence.py          # voice stayer
  birthdays.py
  economy.py
  roulette.py / slots.py
  backup.py            # core backup cog
  backup_admin.py      # optional admin patches (icon, as_of, prune)
  twitch_mirror.py     # Twitch ↔ Discord mirror + catch-up
  badgebase.py
  status.py            # /bot-status unified overview
utils/                 # shared helpers, stores, mixins
data/                  # runtime DBs (gitignored)
```

## Quick start

```bash
cp .env.example .env
# fill DISCORD_TOKEN and optional feature vars
pip install -r requirements.txt
python bot.py
```

Docker / Container Station: build from the included Dockerfile or compose file as on the QNAP.

## Slash commands (overview)

### Core / admin

- `/bot-status` — latency, uptime, cogs, voice, Twitch, backup, economy, BadgeBase

### Economy

Passive earnings: chat coins (hourly cap), voice coins when ≥2 active users in a VC.

### Minigames

- `/roulette <bet>` — color/parity/range bets, adjustable stake  
- `/slots <bet>` — animated slots + replay  

Only the user who ran the command can use the buttons. Others get an ephemeral:  
*Um zu spielen führe den Command bitte selber aus.*

### Backup (administrator)

**Status & sync**

- `/bot-status` — unified overview (backup, Twitch, voice, economy, …)  
- `/backup-backfill` — force-sync: fill history + mark missing IDs as deleted  

**Structure**

- `/backup-snapshot [name]` — roles, channels, permissions (+ icon when patched)  
- `/backup-snapshots` — list snapshots  
- `/backup-restore [snapshot_id] [clear_first] [confirm_clear]` — restore structure  
  - `clear_first:True` requires `confirm_clear:DELETE`  

**Messages**

- `/backup-restore-messages [channel] [limit] [match_by_name] [snapshot_id]`  
  - Webhooks with original name/avatar; timestamps shown in username; no snapshot = all stored messages including deleted (disaster mode when admin patch applies)  

### Twitch mirror

Requires `TWITCH_TOKEN`, `TWITCH_CHANNEL`, `TWITCH_DISCORD_CHANNEL_ID` (and ideally Helix scopes for send/delete).

- IRC idle watchdog reconnects on blackholed sockets (e.g. router gateway change)
- robotty catch-up on reconnect + periodic (defaults: limit 50, interval 300s)

### BadgeBase

- Notifications for newly claimable badges (channel set via command)
- `/badgebase-claimable` and related helpers

## Environment

See `.env.example`. Important keys include `DISCORD_TOKEN`, `LOG_LEVEL`, backup paths, Twitch and BadgeBase settings.

## Troubleshooting

| Symptom | Check |
|---------|--------|
| Voice reconnect loop | Valid `VOICE_CHANNEL_ID`, bot can join |
| Twitch not mirroring | Token, client id, channel name, Discord channel ID; bot mod for deletes |
| Discord→Twitch delete fails | Need Helix send (`user:write:chat`) so message IDs are stored; map is in-memory |
| Backup restore hierarchy odd | Bot role must be highest manageable; use `clear_first` carefully |
| Unexpected command errors | Bot logs show full traceback; users get an ephemeral message |

## Extending

Drop a new `cogs/*.py` with a `setup(bot)` and it loads automatically. Shared UI patterns: `BetAdjustableMixin`, `ReplayMixin`, `OWNER_ONLY_MSG`.

Per-command `@command.error` handlers still override the global tree error handler when present (e.g. cooldowns on roulette/slots).

---

Built for QNAP Container Station — persistent voice, birthdays, economy, disaster-ready backup, and optional Twitch chat bridging.
