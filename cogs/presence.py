from __future__ import annotations

from discord.ext import commands, tasks
from discord import Activity, ActivityType


class PresenceCog(commands.Cog):
    """Dynamische Rich Presence mit nächstem Geburtstag + Gesamtanzahl."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.update_presence.start()

    async def cog_unload(self) -> None:
        self.update_presence.cancel()

    @tasks.loop(minutes=60)
    async def update_presence(self) -> None:
        """Aktualisiert die Rich Presence alle 60 Minuten."""
        birthday_cog = self.bot.get_cog("BirthdayCog")
        if not birthday_cog or not self.bot.guilds:
            return

        guild = self.bot.guilds[0]
        info = birthday_cog.get_next_birthday_info(guild.id)  # type: ignore[attr-defined]

        if not info:
            activity = Activity(
                type=ActivityType.watching,
                name="Geburtstage 🎂"
            )
            await self.bot.change_presence(activity=activity)
            return

        name: str = info.get("name", "Jemand")
        days: int = info.get("days_until", 0)
        total: int = info.get("total", 0)

        if days == 0:
            text = f"Heute hat {name} Geburtstag! 🎉 • {total} Geburtstage"
        elif days == 1:
            text = f"Morgen hat {name} Geburtstag 🎂 • {total} Geburtstage"
        else:
            text = f"Nächster: {name} in {days} Tagen 🎂 • {total} Geburtstage"

        activity = Activity(
            type=ActivityType.watching,
            name=text[:128]
        )
        await self.bot.change_presence(activity=activity)

    @update_presence.before_loop
    async def before_presence_update(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PresenceCog(bot))
