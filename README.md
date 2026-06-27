# Discord QNAP Bot

A Discord bot designed to run on **QNAP NAS** using Docker / Container Station.

**Main purpose**: Keep a voice channel alive 24/7 + expandable features like the Birthday system.

Built with a clean, **cog-based architecture** so you can easily add more features.

## Features

- Voice channel 24/7 stayer (auto-reconnect)
- **Birthday system** with slash commands (`/birthday`)
- JSON storage with **configurable path** (supports multiple independent bots)
- Daily birthday announcements
- Docker-ready for QNAP Container Station with persistent storage
- Easy to expand with more cogs

## Project Structure

```
discord-qnap-bot/
├── bot.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── cogs/
│   ├── voice_stayer.py
│   └── birthdays.py
└── README.md
└── data/          # created automatically (mount as volume!)
```

## Making Birthday Data Persistent (Important!)

The birthday database lives in `data/birthdays.json`.

**By default it is inside the container** and will be lost when you rebuild or recreate the container.

### Recommended: Add volume mount (docker-compose)

Update your `docker-compose.yml` (already done in the repo):

```yaml
volumes:
  - ./data:/app/data
```

This creates a `data/` folder **on your QNAP host** next to `docker-compose.yml`. All birthday data will survive container restarts, updates, and rebuilds.

After the first run, you can even edit `data/birthdays.json` directly if needed.

### Alternative: Named Docker volume

```yaml
volumes:
  - birthday-data:/app/data
```

Then in Container Station you can manage the named volume.

## Running Multiple Bots on Different Servers (Separate Databases)

You want two completely independent bots (different tokens, different servers, separate birthday databases).

### Recommended approach (cleanest)

1. Create **two separate folders** on your QNAP, for example:
   - `/share/Container/discord-bot-server1/`
   - `/share/Container/discord-bot-server2/`

2. In each folder:
   - Clone or copy the bot files
   - Create its own `.env` with the correct `DISCORD_TOKEN` and `VOICE_CHANNEL_ID`
   - Use its own `docker-compose.yml` (you can copy the one from the repo)

3. Each folder gets its **own `./data` subfolder** thanks to the volume mount.
   → Completely separate `birthdays.json` files. No interference.

This is the simplest and most reliable way on QNAP.

### Alternative (advanced): Use environment variable

You can override the database file path using the environment variable:

```env
BIRTHDAY_DATA_PATH=/app/data/server1_birthdays.json
```

Then mount different host paths or use different named volumes for each container.

This works if you want to run both bots from the **same folder/image** but with different data files.

## Quick Start (same as before)

See the setup instructions in the repo for cloning, `.env`, inviting the bot, and running on QNAP.

## Birthday System (`/birthday` commands)

All commands are **slash commands** (type `/birthday` in Discord).

### User Commands
- **/birthday set** `<date>` `[year]`
  Set your own birthday. Supported date formats:
  - `25-12`, `12/25`, `25/12`
  - `2025-12-25`, `25-12-2025`
  - `December 25`, `25 December`, `Dec 25`

- **/birthday remove** `[user]`
  Remove your own birthday (or someone else's if you have Manage Server permission).

- **/birthday list**
  Shows upcoming birthdays in the server (next ~30 days), sorted by soonest.

- **/birthday today**
  Shows who has a birthday *today*.

### Admin Commands (Manage Server permission required)
- **/birthday setfor** `<user>` `<date>` `[year]`
  Set birthday for any member.

- **/birthday channel** `<#channel>`
  Set the text channel where daily "Happy Birthday" messages will be automatically posted.

### How Daily Announcements Work
- The bot checks once per day and posts in the configured channel (if set).
- Format: "🎉 Happy Birthday today! @user1 (turns 25!)✨, @user2"
- If no channel is set, no automatic messages are sent (you can still use `/birthday today` manually).

**Tip**: After adding the first birthdays, use `/birthday channel #general` (or a dedicated #birthdays channel) so the bot can celebrate automatically.

## Environment Variables

| Variable               | Description                                           | Example                          |
|------------------------|-------------------------------------------------------|----------------------------------|
| `DISCORD_TOKEN`        | Discord Bot Token                                     | `MTIz...`                        |
| `VOICE_CHANNEL_ID`     | Target voice channel to stay in                       | `1234567890123456789`            |
| `BIRTHDAY_DATA_PATH`   | Custom path for birthday JSON file (optional)         | `data/server1_birthdays.json`    |

## Expanding Further

Just drop new `.py` files in `cogs/`. They will be loaded automatically.

Example future cog ideas: music, moderation, reminders, polls, etc.

## Troubleshooting

- Slash commands not appearing? The bot syncs them on startup. Try restarting the container.
- Birthday data disappearing? Make sure the `./data` volume is mounted.
- Date not parsing? Use one of the supported formats listed above.

Happy botting! 🤖
