# Discord QNAP Bot

A Discord bot built for a QNAP (or any Docker) host: voice presence, economy, minigames, full guild backup/restore, Twitch chat mirror, and BadgeBase notifications.

## Features

- **Voice stayer** — join a target voice channel and stay connected
- **Economy** — balances, daily, transfer, leaderboard; chat + voice passive income
- **Minigames** — roulette and slots
- **Backup** — message history, attachments, structure snapshots, restore
- **Twitch mirror** — bidirectional chat mirror with delete sync + robotty catch-up
- **BadgeBase** — claimable badge notifications
- **Birthdays** — birthday tracking and announcements
- **Status** — single `/bot-status` overview for admins

## Requirements

- Python 3.11+
- Discord bot token
- (Optional) Twitch token + client id for the mirror
- (Optional) BadgeBase API key

## Quick start

```bash
cp .env.example .env
# edit .env
pip install -r requirements.txt
python bot.py
```

Or with Docker / docker-compose as used on the QNAP.

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

See source under `cogs/` and `utils/` for full option lists and behaviour.

## Twitch mirror

Requires `TWITCH_TOKEN`, `TWITCH_CHANNEL`, `TWITCH_DISCORD_CHANNEL_ID` (and ideally Helix scopes for send/delete).

- IRC idle watchdog reconnects on blackholed sockets (e.g. router gateway change)
- robotty catch-up on reconnect + periodic (defaults: limit 50, interval 300s)

## Environment

See `.env.example`. Important keys include `DISCORD_TOKEN`, `LOG_LEVEL`, backup paths, Twitch and BadgeBase settings.

## License

Private / as used by the project owner.
