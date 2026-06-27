# Discord QNAP Bot

A Discord bot designed to run on **QNAP NAS** using Docker / Container Station.

**Main purpose**: Keep a voice channel alive 24/7 (prevents the VC from becoming inactive).

Built with a clean, **cog-based architecture** so you can easily expand it later (e.g. add a full Birthday system with slash commands).

## Features

- Stays connected to a target voice channel indefinitely
- Automatic reconnection if the bot gets disconnected
- Restart-safe (Docker `unless-stopped` policy)
- Expandable cog system (new features = new file in `cogs/`)
- Full Docker + docker-compose setup optimized for QNAP
- Environment variable based configuration (no secrets in code)

## Project Structure

```
discord-qnap-bot/
├── bot.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── cogs/
│   └── voice_stayer.py
└── README.md
```

## Quick Start

### 1. Clone & prepare

```bash
git clone https://github.com/SonIcX154/discord-qnap-bot.git
cd discord-qnap-bot
cp .env.example .env
```

### 2. Configure `.env`

Edit the `.env` file:

```env
DISCORD_TOKEN=your_bot_token_here
VOICE_CHANNEL_ID=1234567890123456789
```

**How to get the Voice Channel ID**:
1. Enable **Developer Mode** in Discord (`Settings` → `Advanced` → `Developer Mode`)
2. Right-click the voice channel → **Copy Channel ID**

### 3. Invite the bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Select your application → **OAuth2** → **URL Generator**
3. Scopes: `bot` + `applications.commands`
4. Bot Permissions (minimum):
   - `View Channels`
   - `Connect` (Voice)
   - `Speak` (if you plan to add audio later)
5. Copy the generated URL and invite the bot to your server

### 4. Run on QNAP NAS (Container Station)

#### Recommended: Using docker-compose

1. Copy/upload the entire folder to your QNAP (e.g. `/share/Container/discord-qnap-bot/`)
2. SSH into the NAS or open **Container Station**
3. Create the `.env` file on the NAS with your real values
4. Run:
   ```bash
   cd /share/Container/discord-qnap-bot
   docker-compose up -d --build
   ```

#### Alternative: Container Station GUI

- Use the **Compose** tab (if available) and paste the content of `docker-compose.yml`
- Or create a new container from the `Dockerfile`
- Set **Restart policy** to `unless-stopped`
- Add the two environment variables (or mount `.env`)
- Start the container

Check the container logs. You should see:
- `VoiceStayer initialized...`
- `✅ [VoiceStayer] Connected to voice channel: ...`

The bot will now stay in that voice channel 24/7.

## Local Development / Testing

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python bot.py
```

## How to Expand (Future Birthday System, etc.)

1. Create a new file: `cogs/birthdays.py`
2. Write a standard discord.py Cog (you can use `@app_commands.command()` for slash commands)
3. The cog will be **automatically loaded** on next container restart
4. For slash commands, add this in `bot.py` `on_ready` (or a admin command):
   ```python
   await bot.tree.sync()
   ```

Example future cog skeleton:
```python
from discord.ext import commands
from discord import app_commands

class BirthdayCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="birthday-set")
    async def set_birthday(self, interaction: discord.Interaction, user: discord.Member, date: str):
        await interaction.response.send_message(f"Birthday set for {user.mention}!")

async def setup(bot):
    await bot.add_cog(BirthdayCog(bot))
```

## Environment Variables

| Variable            | Description                              | Example                  |
|---------------------|------------------------------------------|--------------------------|
| `DISCORD_TOKEN`     | Discord Bot Token from Developer Portal  | `MTIz...`                |
| `VOICE_CHANNEL_ID`  | Target voice channel to stay in          | `1234567890123456789`    |

## Troubleshooting

- **Bot not joining VC** → Check logs, confirm `VOICE_CHANNEL_ID` is correct, and that the bot has `Connect` permission in the channel.
- **Token error** → Make sure `.env` has no extra spaces or quotes around values.
- **On QNAP** → Ensure the container has outbound internet access (port 443).
- **Reconnects too often** → Normal behavior if someone kicks the bot; it will rejoin automatically.

## Next Steps

- Add more cogs for slash commands
- Add persistent storage (SQLite / JSON) for birthday data
- Add logging to a file + volume mount

Happy botting! 🤖
