import os
import datetime
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True
intents.members = True


class QNAPBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None
        )
        self.synced = False

    async def setup_hook(self):
        """Load all cogs automatically on startup."""
        print("Loading cogs...")
        cogs_dir = "./cogs"
        if os.path.exists(cogs_dir):
            for filename in os.listdir(cogs_dir):
                if filename.endswith(".py") and not filename.startswith("_"):
                    extension = f"cogs.{filename[:-3]}"
                    try:
                        await self.load_extension(extension)
                        print(f"  ✅ Loaded cog: {extension}")
                    except Exception as e:
                        print(f"  ❌ Failed to load cog {extension}: {e}")
        else:
            print("No cogs directory found.")
        print("Cog loading complete.")

    async def on_ready(self):
        if self.user is None:
            return

        print(f"🤖 Logged in as {self.user} (ID: {self.user.id})")
        print(f"Connected to {len(self.guilds)} guilds.")

        # Log current container time (helpful for verifying timezone with scheduled tasks)
        now = datetime.datetime.now()
        print(f"[System] Current container time after startup: {now.strftime('%Y-%m-%d %H:%M:%S')} (local time, TZ from environment)")

        # Sync slash commands (safe to run on every startup for small bots)
        if not self.synced:
            try:
                synced = await self.tree.sync()
                print(f"🔄 Synced {len(synced)} slash command(s)")
                self.synced = True
            except Exception as e:
                print(f"Failed to sync slash commands: {e}")


bot = QNAPBot()


async def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("ERROR: DISCORD_TOKEN not found in .env file!")
        return
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
