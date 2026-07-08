import discord
from discord.ext import commands, tasks
from discord import Activity, ActivityType
from datetime import date


class PresenceCog(commands.Cog):
    """Dynamische Rich Presence mit nächstem Geburtstag + Gesamtanzahl."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.update_presence.start()

    def cog_unload(self):
        self.update_presence.cancel()

    @tasks.loop(minutes=60)
    async def update_presence(self):
        """Aktualisiert die Rich Presence alle 60 Minuten."""
        birthday_cog = self.bot.get_cog("BirthdayCog")
        if not birthday_cog or not self.bot.guilds:
            return

        guild = self.bot.guilds[0]  # Da pro Server ein Bot
        gdata = birthday_cog.data.get(str(guild.id), {})

        # Gesamtzahl der Geburtstage (ohne config)
        total_birthdays = len([k for k in gdata if k != "config"])

        if total_birthdays == 0:
            activity = Activity(
                type=ActivityType.watching,
                name="Geburtstage 🎂"
            )
            await self.bot.change_presence(activity=activity)
            return

        # Nächsten Geburtstag finden
        today = date.today()
        next_name = "Jemand"
        days_until_next = 999

        for uid_str, entry in gdata.items():
            if uid_str == "config":
                continue
            try:
                month = entry.get("month")
                day = entry.get("day")
                if month is None or day is None:
                    continue

                d_until, _ = birthday_cog._get_days_until(month, day, today)

                if d_until < days_until_next:
                    days_until_next = d_until
                    member = guild.get_member(int(uid_str))
                    next_name = member.display_name if member else uid_str
            except Exception:
                continue

        # Rich Presence Text bauen
        if days_until_next == 0:
            text = f"Heute hat {next_name} Geburtstag! 🎉 • {total_birthdays} Geburtstage"
        elif days_until_next == 1:
            text = f"Morgen hat {next_name} Geburtstag 🎂 • {total_birthdays} Geburtstage"
        else:
            text = f"Nächster: {next_name} in {days_until_next} Tagen 🎂 • {total_birthdays} Geburtstage"

        # Discord limitiert Activity Name auf 128 Zeichen
        activity = Activity(
            type=ActivityType.watching,
            name=text[:128]
        )
        await self.bot.change_presence(activity=activity)

    @update_presence.before_loop
    async def before_presence_update(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(PresenceCog(bot))
