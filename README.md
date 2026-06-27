# Discord QNAP Bot

A lightweight Discord bot built for QNAP Container Station.

It keeps a single voice channel connected permanently and provides a birthday tracking system with slash commands.

## What it does

- Keeps a voice channel alive 24/7 with automatic reconnect
- Stores birthdays per server in JSON
- Supports slash commands for birthday management
- Sends daily birthday announcements to a configured channel
- Uses a cog-based architecture for easy extension

## Repo contents

- `bot.py` — main bot launcher and cog loader
- `cogs/voice_stayer.py` — voice channel persistence logic
- `cogs/birthdays.py` — birthday commands and JSON storage
- `Dockerfile` — container image build instructions
- `docker-compose.yml` — local deployment config with persistent volume
- `requirements.txt` — Python dependencies
- `data/` — runtime data storage location for birthdays
- `.env.example` — example environment variables

## Requirements

- Docker (or Docker-compatible Container Station on QNAP)
- A Discord bot token with proper permissions
- A target voice channel ID where the bot should remain connected

## Setup

1. Copy `.env.example` to `.env`.
2. Add your Discord bot token and voice channel ID.
3. If you want birthday storage to persist, keep the volume mount in `docker-compose.yml`.

Example `.env`:

```env
DISCORD_TOKEN=your_bot_token_here
VOICE_CHANNEL_ID=1234567890123456789
```

Optional:

```env
BIRTHDAY_DATA_PATH=data/birthdays.json
```

## Running the bot

### With Docker Compose

```bash
docker compose up -d
```

The service will mount `./data` into `/app/data`, so `birthdays.json` is stored outside the container.

### Without Docker

If you want to run locally in Python:

```bash
python -m pip install -r requirements.txt
python bot.py
```

## Environment variables

| Variable             | Required | Purpose |
|----------------------|----------|---------|
| `DISCORD_TOKEN`      | yes      | Discord bot token |
| `VOICE_CHANNEL_ID`   | yes      | Voice channel to keep connected |
| `BIRTHDAY_DATA_PATH` | no       | Path to birthday JSON file |

## Birthday commands

The bot registers slash commands in Discord. Use them by typing `/birthday`.

### User commands

- `/birthday-set date [year]`
  - Save your birthday.
  - Supported formats: `25-12`, `12/25`, `25/12`, `2025-12-25`, `25-12-2025`, `December 25`, `25 December`, `Dec 25`.
- `/birthday-remove [user]`
  - Remove your own birthday or another user's birthday if you have permission.
- `/birthday-list`
  - Show upcoming birthdays in the server.
- `/birthday-today`
  - Show who has a birthday today.

### Admin commands

- `/birthday-setfor user date [year]`
  - Set a birthday for another member.
- `/birthday-channel channel`
  - Set the announcement channel for daily birthday messages.

## Birthday announcements

If a channel is configured with `/birthday-channel`, the bot will post a daily message when someone has a birthday.

Example message:

```text
🎉 Happy Birthday today! @User1 (turns 25!)✨, @User2
```

If no channel is configured, the bot will not send automatic announcements.

## Voice stayer behavior

- Reads `VOICE_CHANNEL_ID` from `.env`
- Starts a background task when the cog loads
- Every 10 seconds, it checks whether the bot is connected to the correct voice channel
- If disconnected or connected to the wrong channel, it reconnects automatically

## Data persistence

Birthday data is stored in JSON at `data/birthdays.json` by default.

Because `docker-compose.yml` mounts `./data:/app/data`, the file is preserved across container restarts.

If you want separate databases for multiple bots, either:

- run each bot from a different folder with its own `./data` volume, or
- set `BIRTHDAY_DATA_PATH` to a unique file path per bot

## Deployment notes for QNAP

- Use the repository with Container Station or Docker Compose.
- Ensure `./data` is mounted as a persistent volume.
- Restart the container after updating the `.env` or bot code.

## Troubleshooting

- `DISCORD_TOKEN` missing: create `.env` from `.env.example`
- Slash commands missing: restart the bot and wait for command sync
- Birthdays not saved: verify `./data` volume mount and file permissions
- Voice reconnecting repeatedly: confirm `VOICE_CHANNEL_ID` points to a valid voice channel

## Extending the bot

To add new functionality, create a new cog in `cogs/`. The bot automatically loads any `.py` file in that folder.

Example ideas:

- moderation commands
- reminders and events
- music playback
- polls or giveaways

---

Built for QNAP Container Station with a focus on persistent voice channel uptime and easy birthday tracking.
