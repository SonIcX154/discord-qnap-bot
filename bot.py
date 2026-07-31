from __future__ import annotations

import os
import sys
import time
import logging
import datetime
import traceback

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
# Keep discord.py noise down unless debugging
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)
logging.getLogger("discord.gateway").setLevel(logging.WARNING)

log = logging.getLogger("qnapbot")

intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True
intents.members = True
intents.message_content = True  # Needed for BackupCog (reading message content)


class QNAPBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )
        self.synced: bool = False
        self.start_time: float = time.time()

    async def setup_hook(self) -> None:
        """Load all cogs automatically on startup."""
        log.info("Loading cogs…")
        cogs_dir = "./cogs"
        if os.path.exists(cogs_dir):
            for filename in sorted(os.listdir(cogs_dir)):
                if filename.endswith(".py") and not filename.startswith("_"):
                    extension = f"cogs.{filename[:-3]}"
                    try:
                        await self.load_extension(extension)
                        log.info("  ✅ Loaded cog: %s", extension)
                    except Exception:
                        log.exception("  ❌ Failed to load cog %s", extension)
        else:
            log.warning("No cogs directory found.")
        log.info("Cog loading complete.")

        # Global slash-command error handler (local @command.error still wins)
        self.tree.error(self.on_app_command_error)

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """User-facing ephemeral errors + full traceback in logs."""
        original = getattr(error, "original", None)

        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"⏳ Warte noch **{error.retry_after:.1f}s**."
        elif isinstance(error, app_commands.MissingPermissions):
            missing = ", ".join(error.missing_permissions) if error.missing_permissions else "?"
            msg = f"❌ Dafür fehlen dir Berechtigungen (`{missing}`)."
        elif isinstance(error, app_commands.BotMissingPermissions):
            missing = ", ".join(error.missing_permissions) if error.missing_permissions else "?"
            msg = f"❌ Mir fehlen Berechtigungen (`{missing}`)."
        elif isinstance(error, app_commands.CheckFailure):
            msg = "❌ Du darfst diesen Command nicht nutzen."
        elif isinstance(error, app_commands.CommandNotFound):
            return
        else:
            cmd = interaction.command.name if interaction.command else "?"
            log.error(
                "App command error in /%s by %s (%s): %s",
                cmd,
                interaction.user,
                interaction.user.id,
                error,
            )
            if original is not None:
                log.error(
                    "".join(
                        traceback.format_exception(
                            type(original), original, original.__traceback__
                        )
                    )
                )
            else:
                log.error(
                    "".join(
                        traceback.format_exception(type(error), error, error.__traceback__)
                    )
                )
            msg = "❌ Unerwarteter Fehler. Details stehen im Bot-Log."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            log.exception("Failed to send app command error message")

    async def on_ready(self) -> None:
        if self.user is None:
            return

        # Reset uptime baseline on (re)connect so it stays meaningful
        self.start_time = time.time()

        log.info("🤖 Logged in as %s (ID: %s)", self.user, self.user.id)
        log.info("Connected to %s guild(s).", len(self.guilds))

        now = datetime.datetime.now()
        log.info(
            "[System] Container time: %s (local, TZ from environment)",
            now.strftime("%Y-%m-%d %H:%M:%S"),
        )

        if not self.synced:
            try:
                synced = await self.tree.sync()
                log.info("🔄 Synced %s slash command(s)", len(synced))
                self.synced = True
            except Exception:
                log.exception("Failed to sync slash commands")


bot = QNAPBot()


async def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        log.error("DISCORD_TOKEN not found in .env file!")
        return
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
