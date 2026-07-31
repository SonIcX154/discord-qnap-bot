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
bot.py                 # launcher, intents, cog auto-load, slash sync
cogs/
  backup.py            # message logging, backfill, snapshots, restore
  backup_admin.py      # purge, excludes, retention, restore patches
  birthdays.py         # birthday commands + daily task
  economy.py           # coins, coinflip, leaderboard, give/beg
  roulette.py          # roulette minigame
  slots.py             # slots minigame
  twitch_mirror.py     # Twitch ↔ Discord mirror
  voice_stayer.py      # permanent voice connection
  presence.py          # bot presence
utils/
  backup_ops.py        # DB helpers, purge, excludes
  message_restore.py   # webhook message restore
  structure_helpers.py # role hierarchy, branding, clear helpers
  bet_mixin.py         # shared bet +/- buttons + OWNER_ONLY_MSG
  replay_mixin.py      # "Nochmal spielen" button
scripts/               # rebuild helpers for QNAP
Dockerfile
docker-compose.yml     # TZ=Europe/Berlin, ./data volume
.env.example
requirements.txt
```

## Requirements

- Docker (or Container Station on QNAP)
- Discord bot token with intents: **Server Members**, **Message Content**, and usual guild/voice intents
- Bot permissions as needed for each feature (Manage Channels/Roles for restore, Manage Webhooks for message restore / Twitch mirror, etc.)

## Quick start

1. Copy `.env.example` → `.env` and fill at least `DISCORD_TOKEN` and `VOICE_CHANNEL_ID`.
2. Start:

```bash
docker compose up -d --build
```

Data lives in `./data` (mounted to `/app/data`).

Without Docker:

```bash
python -m pip install -r requirements.txt
python bot.py
```

## Environment variables

### Core

| Variable | Required | Purpose |
|----------|----------|---------|
| `DISCORD_TOKEN` | yes | Discord bot token |
| `VOICE_CHANNEL_ID` | yes* | Voice channel to stay in (`*` if you use voice stayer) |
| `TZ` | no | Set in `docker-compose.yml` (default `Europe/Berlin`) for schedules & timestamps |

### Birthdays

| Variable | Default | Purpose |
|----------|---------|---------|
| `BIRTHDAY_DATA_PATH` | `data/birthdays.json` | Birthday JSON path |
| `BIRTHDAY_ANNOUNCE_HOUR` | `0` | Daily announce hour (0–23) |
| `BIRTHDAY_ANNOUNCE_MINUTE` | `0` | Daily announce minute |

### Economy / games

| Variable | Default | Purpose |
|----------|---------|---------|
| `ECONOMY_DATA_PATH` | `data/economy.db` | Economy SQLite DB |
| `ROULETTE_EMOTE` | `🎰` | Emote prefix for roulette embeds |

### Backup

| Variable | Default | Purpose |
|----------|---------|---------|
| `BACKUP_DATA_PATH` | `data/backup.db` | Backup SQLite DB |
| `BACKUP_ATTACHMENTS_PATH` | `data/backups/attachments` | Downloaded attachment files |
| `BACKUP_SOFT_DELETE_DAYS` | `30` | Hard-delete soft-deleted rows after N days |
| `BACKUP_PURGE_INTERVAL_HOURS` | `24` | How often the retention loop runs |

### Twitch mirror (optional)

| Variable | Purpose |
|----------|---------|
| `TWITCH_TOKEN` | OAuth token (`oauth:…` or bare) |
| `TWITCH_CLIENT_ID` | Twitch application client ID |
| `TWITCH_CHANNEL` | Channel login to join (e.g. `ich_klau_gratis_brot`) |
| `TWITCH_DISCORD_CHANNEL_ID` | Discord text channel for the mirror |
| `TWITCH_NICK` | Bot account login (if different from token user) |
| `TWITCH_MIRROR_DELAY` | Delay between mirrored posts (default `0.35`) |
| `TWITCH_DISCORD_TO_TWITCH` | `1` / `0` — enable Discord → Twitch |
| `DISCORD_OWNER_ID` | Your Discord user ID for owner pings |
| `TWITCH_OWNER_NAMES` | Comma-separated Twitch names that map to the Discord ping |

**Twitch scopes (mirror + delete):**  
`chat:read` `chat:edit` `user:write:chat` `moderator:manage:chat_messages`  
Bot account should be a **mod** in the channel.

See `.env.example` for commented templates.

---

## Commands overview

### Birthdays

**Users**

- `/geburtstag-setzen <datum> [jahr]` — set your birthday  
- `/geburtstag-entfernen [benutzer]` — remove birthday  
- `/geburtstags-liste` — upcoming birthdays  
- `/geburtstag-heute` — today’s birthdays  

**Admins**

- `/birthday-setfor <benutzer> <datum> [jahr]`  
- `/birthday-channel <channel>` — announcement channel  
- `/test-birthday-messages` — test announce messages  

### Economy

- `/balance [user]` — balance  
- `/daily` — daily bonus (once per calendar day, local TZ)  
- `/give <user> <amount>` — transfer coins  
- `/beg` — beg; others can donate via button  
- `/leaderboard` — paginated leaderboard  
- `/coinflip <bet>` — Kopf/Zahl (min. 10)  
- `/set-currency`, `/economy-give`, `/economy-take`, `/economy-set` — admin  

Passive earnings: chat coins (hourly cap), voice coins when ≥2 active users in a VC.

### Minigames

- `/roulette <bet>` — color/parity/range bets, adjustable stake  
- `/slots <bet>` — animated slots + replay  

Only the user who ran the command can use the buttons. Others get an ephemeral:  
*Um zu spielen führe den Command bitte selber aus.*

### Backup (administrator)

**Status & sync**

- `/backup-status` — DB counts, attachments, running jobs  
- `/backup-backfill` — force-sync: fill history + mark missing IDs as deleted  

**Structure**

- `/backup-snapshot [name]` — roles, channels, permissions (+ icon when patched)  
- `/backup-snapshots` — list snapshots  
- `/backup-restore [snapshot_id] [clear_first] [confirm_clear]` — restore structure  
  - `clear_first:True` requires `confirm_clear:DELETE`  

**Messages**

- `/backup-restore-messages [channel] [limit] [match_by_name] [snapshot_id]`  
  - Webhooks with original name/avatar; timestamps shown in username; no snapshot = all stored messages including deleted (disaster mode when admin patch applies)  

**Maintenance**

- `/backup-purge` — hard-delete soft-deleted and/or excluded messages (`confirm:PURGE`)  
- `/backup-exclude` / `/backup-unexclude` / `/backup-excludes` — per channel  
- `/backup-exclude-guild` / `/backup-include-guild` / `/backup-excluded-guilds`  
- `/backup-download-missing` — re-download attachments from stored CDN URLs  

**Behaviour notes**

- New/edited messages are logged continuously (excluded guilds/channels skipped).  
- Deletes set `is_deleted=1` (+ `deleted_at`). Auto-purge after ~30 days (configurable).  
- Force-sync is the “ultimate” DB reconcile against live Discord history.  

### Twitch mirror

No slash commands required once env is set. On startup the cog joins the Twitch channel and mirrors into the Discord channel via webhook (display name + avatar).

- **Twitch → Discord:** chat, `/me` as plain text, replies Chatterino-style, CLEARMSG/CLEARCHAT → Discord deletes  
- **Discord → Twitch:** human messages as `[Discord] Name: …` (Helix when scopes allow)  
- **Deletes both ways** while message IDs are still in the in-memory map (~last 3000; lost on restart)  
- Owner name(s) in Twitch chat → Discord mention via `DISCORD_OWNER_ID`  

---

## Docker / QNAP notes

- `docker-compose.yml` sets `TZ=Europe/Berlin` and mounts `./data:/app/data`.  
- Rebuild after code or env changes: `docker compose up -d --build` (or your `scripts/rebuild-*.sh`).  
- Slash commands sync on each startup (fine for a small private bot).  
- For multiple bot instances, use separate folders / data paths (`BIRTHDAY_DATA_PATH`, `ECONOMY_DATA_PATH`, `BACKUP_DATA_PATH`).  

## Troubleshooting

| Symptom | Check |
|---------|--------|
| Bot won’t start | `DISCORD_TOKEN` in `.env` |
| Slash commands missing | Restart; wait a minute for global sync |
| Wrong times (birthdays / message restore stamps) | Container `TZ` |
- Birthdays not saving | `./data` mount + permissions |
| Voice reconnect loop | Valid `VOICE_CHANNEL_ID`, bot can join |
| Twitch not mirroring | Token, client id, channel name, Discord channel ID; bot mod for deletes |
| Discord→Twitch delete fails | Need Helix send (`user:write:chat`) so message IDs are stored; map is in-memory |
| Backup restore hierarchy odd | Bot role must be highest manageable; use `clear_first` carefully |

## Extending

Drop a new `cogs/*.py` with a `setup(bot)` and it loads automatically. Shared UI patterns: `BetAdjustableMixin`, `ReplayMixin`, `OWNER_ONLY_MSG`.

---

Built for QNAP Container Station — persistent voice, birthdays, economy, disaster-ready backup, and optional Twitch chat bridging.
