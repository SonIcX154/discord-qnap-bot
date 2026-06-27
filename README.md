# Discord QNAP Bot

A Discord bot designed to run on **QNAP NAS** using Docker / Container Station.

**Main purpose**: Keep a voice channel alive 24/7 + expandable features like the Birthday system.

Built with a clean, **cog-based architecture** so you can easily add more features.

## Features

- Voice channel 24/7 stayer (auto-reconnect)
- **Birthday system** with slash commands (`/birthday`)
- JSON storage + daily announcements
- Docker-ready for QNAP Container Station
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
```

## Quick Start (same as before)

See the previous setup instructions for cloning, `.env`, inviting the bot, and running on QNAP.

**Important for Birthday data persistence**:
Add this volume to your `docker-compose.yml` (or Container Station) so birthdays survive container updates:

```yaml
volumes:
  - ./data:/app/data
```

After adding the volume, create the `data/` folder on the host if needed.

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

| Variable            | Description                              | Example                  |
|---------------------|------------------------------------------|--------------------------|
| `DISCORD_TOKEN`     | Discord Bot Token                        | `MTIz...`                |
| `VOICE_CHANNEL_ID`  | Target voice channel to stay in          | `1234567890123456789`    |

## Expanding Further

Just drop new `.py` files in `cogs/`. They will be loaded automatically.

Example future cog ideas: music, moderation, reminders, polls, etc.

## Troubleshooting

- Slash commands not appearing? The bot syncs them on startup. Try restarting the container.
- Birthday data disappearing? Make sure you mounted the `./data` volume in Docker.
- Date not parsing? Use one of the supported formats listed above.

Happy botting! 🤖
