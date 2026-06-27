import json
import os
import datetime
from datetime import date, timedelta
import discord
from discord.ext import commands, tasks
from discord import app_commands


# Configurable via environment variable for multiple bot instances
DATA_FILE = os.getenv("BIRTHDAY_DATA_PATH", "data/birthdays.json")


class BirthdayCog(commands.Cog):
    """Birthday system with slash commands.
    
    Features:
    - /birthday set, setfor, remove, list, today, channel
    - JSON storage (path configurable via BIRTHDAY_DATA_PATH)
    - Daily birthday announcements in configured channel
    - Age calculation when birth year is provided
    """

    birthday = app_commands.Group(
        name="birthday",
        description="Birthday management and announcements"
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data: dict = {}  # guild_id (str) -> {user_id (str): {month, day, year?}, "config": {...}}
        self._load_data()

    def _load_data(self):
        data_dir = os.path.dirname(DATA_FILE) or "."
        os.makedirs(data_dir, exist_ok=True)
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {}
        else:
            self.data = {}

    def _save_data(self):
        data_dir = os.path.dirname(DATA_FILE) or "."
        os.makedirs(data_dir, exist_ok=True)
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[Birthday] Failed to save data: {e}")

    def _get_guild_data(self, guild_id: int) -> dict:
        gid = str(guild_id)
        if gid not in self.data:
            self.data[gid] = {}
        return self.data[gid]

    def _parse_date(self, date_str: str) -> tuple[int, int] | None:
        """Try multiple common date formats. Returns (month, day) or None."""
        if not date_str:
            return None
        s = date_str.strip()
        # Full date formats (we only care about month/day)
        full_formats = [
            "%Y-%m-%d", "%d-%m-%Y", "%m-%d-%Y",
            "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"
        ]
        for fmt in full_formats:
            try:
                dt = datetime.datetime.strptime(s, fmt)
                return dt.month, dt.day
            except ValueError:
                continue
        # Month/day only
        md_formats = ["%d-%m", "%d/%m", "%m-%d", "%m/%d"]
        for fmt in md_formats:
            try:
                dt = datetime.datetime.strptime(s, fmt)
                return dt.month, dt.day
            except ValueError:
                continue
        # Named months (English) - works with strptime %B / %b
        named = s.title()
        named_formats = ["%d %B", "%B %d", "%d %b", "%b %d"]
        for fmt in named_formats:
            try:
                dt = datetime.datetime.strptime(named, fmt)
                return dt.month, dt.day
            except ValueError:
                continue
        return None

    def _get_days_until(self, month: int, day: int, today: date) -> tuple[int, date]:
        """Return (days_until_next_birthday, next_birthday_date)"""
        try:
            this_year = date(today.year, month, day)
        except ValueError:
            # Feb 29 on non-leap year -> treat as Feb 28
            this_year = date(today.year, month, min(day, 28))
        if this_year < today:
            try:
                next_bd = date(today.year + 1, month, day)
            except ValueError:
                next_bd = date(today.year + 1, month, min(day, 28))
        else:
            next_bd = this_year
        days = (next_bd - today).days
        return days, next_bd

    async def _announce_birthdays(self, check_date: date):
        """Announce birthdays for the given date in configured channels."""
        for guild in self.bot.guilds:
            gid = str(guild.id)
            gdata = self.data.get(gid, {})
            config = gdata.get("config", {})
            channel_id = config.get("announce_channel_id")
            if not channel_id:
                continue
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue

            celebrants = []
            for uid_str, b in gdata.items():
                if uid_str == "config":
                    continue
                try:
                    if b.get("month") == check_date.month and b.get("day") == check_date.day:
                        member = guild.get_member(int(uid_str))
                        if member:
                            age_str = ""
                            if b.get("year"):
                                try:
                                    age = check_date.year - int(b["year"])
                                    if age > 0:
                                        age_str = f" (turns {age}!)✨"
                                except:
                                    pass
                            celebrants.append(f"{member.mention}{age_str}")
                except Exception:
                    continue

            if celebrants:
                msg = "🎉 **Happy Birthday today!** " + ", ".join(celebrants)
                try:
                    await channel.send(msg)
                except Exception:
                    pass

    # ==================== SLASH COMMANDS ====================

    @birthday.command(name="set", description="Set your own birthday")
    @app_commands.describe(
        date="Date in formats like 25-12, 12/25, 2025-12-25, December 25, 25 December",
        year="Birth year (optional - used for age in announcements)"
    )
    async def set_birthday(
        self,
        interaction: discord.Interaction,
        date: str,
        year: app_commands.Range[int, 1900, 2026] = None
    ):
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        parsed = self._parse_date(date)
        if not parsed:
            await interaction.response.send_message(
                "Could not understand the date. Try formats like `25-12`, `December 25`, or `2025-12-25`.",
                ephemeral=True
            )
            return

        month, day = parsed
        gdata = self._get_guild_data(interaction.guild.id)
        gdata[str(interaction.user.id)] = {
            "month": month,
            "day": day,
            "year": year
        }
        self._save_data()

        age_info = f" (born in {year})" if year else ""
        await interaction.response.send_message(
            f"✅ Your birthday has been set to **{day:02d}-{month:02d}**{age_info}.",
            ephemeral=True
        )

    @birthday.command(name="setfor", description="Set birthday for another member (Admin)")
    @app_commands.describe(
        user="The member whose birthday you want to set",
        date="Date in formats like 25-12, December 25, etc.",
        year="Birth year (optional)"
    )
    @app_commands.default_permissions(manage_guild=True)
    async def setfor(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        date: str,
        year: app_commands.Range[int, 1900, 2026] = None
    ):
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        parsed = self._parse_date(date)
        if not parsed:
            await interaction.response.send_message("Could not parse the date.", ephemeral=True)
            return

        month, day = parsed
        gdata = self._get_guild_data(interaction.guild.id)
        gdata[str(user.id)] = {
            "month": month,
            "day": day,
            "year": year
        }
        self._save_data()

        age_info = f" (born in {year})" if year else ""
        await interaction.response.send_message(
            f"✅ Birthday for {user.mention} set to **{day:02d}-{month:02d}**{age_info}.",
            ephemeral=True
        )

    @birthday.command(name="remove", description="Remove a birthday (your own or someone else's if you have permissions)")
    @app_commands.describe(user="Leave empty to remove your own birthday")
    async def remove_birthday(
        self,
        interaction: discord.Interaction,
        user: discord.Member = None
    ):
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        target = user or interaction.user
        gdata = self._get_guild_data(interaction.guild.id)
        uid = str(target.id)

        if uid not in gdata or uid == "config":
            await interaction.response.send_message(
                f"No birthday found for {target.mention}.", ephemeral=True
            )
            return

        # Permission check if removing someone else's
        if target.id != interaction.user.id:
            if not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message(
                    "You need Manage Server permission to remove someone else's birthday.",
                    ephemeral=True
                )
                return

        del gdata[uid]
        self._save_data()
        await interaction.response.send_message(
            f"✅ Removed birthday for {target.mention}.",
            ephemeral=True
        )

    @birthday.command(name="list", description="Show upcoming birthdays in this server")
    async def list_birthdays(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        gdata = self._get_guild_data(interaction.guild.id)
        today = date.today()
        upcoming = []

        for uid_str, b in gdata.items():
            if uid_str == "config":
                continue
            try:
                month = b["month"]
                day = b["day"]
                days_until, next_date = self._get_days_until(month, day, today)
                member = interaction.guild.get_member(int(uid_str))
                name = member.display_name if member else f"User {uid_str}"
                age_str = ""
                if b.get("year") and member:
                    try:
                        age = today.year - int(b["year"])
                        if age > 0:
                            age_str = f" ({age} years old)"
                    except:
                        pass
                upcoming.append((days_until, f"**{name}** — {next_date.strftime('%d %b %Y')}{age_str}"))
            except Exception:
                continue

        if not upcoming:
            await interaction.response.send_message("No birthdays have been set in this server yet.", ephemeral=True)
            return

        upcoming.sort(key=lambda x: x[0])  # sort by days until
        lines = [f"{d} days — {text}" for d, text in upcoming[:15]]  # limit to 15

        embed = discord.Embed(
            title="🎂 Upcoming Birthdays",
            description="\n".join(lines),
            color=discord.Color.pink()
        )
        embed.set_footer(text=f"Showing next {min(len(upcoming), 15)} birthdays • Use /birthday today for today's celebrants")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @birthday.command(name="today", description="Who has a birthday today?")
    async def today_birthdays(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        gdata = self._get_guild_data(interaction.guild.id)
        today = date.today()
        celebrants = []

        for uid_str, b in gdata.items():
            if uid_str == "config":
                continue
            if b.get("month") == today.month and b.get("day") == today.day:
                member = interaction.guild.get_member(int(uid_str))
                if member:
                    age_str = ""
                    if b.get("year"):
                        try:
                            age = today.year - int(b["year"])
                            if age > 0:
                                age_str = f" (turns {age}!)✨"
                        except:
                            pass
                    celebrants.append(f"{member.mention}{age_str}")

        if not celebrants:
            await interaction.response.send_message("No birthdays today in this server. 😢", ephemeral=True)
            return

        embed = discord.Embed(
            title="🎉 Happy Birthday Today!",
            description=", ".join(celebrants),
            color=discord.Color.gold()
        )
        await interaction.response.send_message(embed=embed)

    @birthday.command(name="channel", description="Set the channel for daily birthday announcements (Admin only)")
    @app_commands.describe(channel="The text channel where birthday messages should be posted")
    @app_commands.default_permissions(manage_guild=True)
    async def set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        gdata = self._get_guild_data(interaction.guild.id)
        if "config" not in gdata:
            gdata["config"] = {}
        gdata["config"]["announce_channel_id"] = channel.id
        self._save_data()

        await interaction.response.send_message(
            f"✅ Daily birthday announcements will now be posted in {channel.mention}.",
            ephemeral=True
        )

    # ==================== BACKGROUND TASK ====================

    @tasks.loop(hours=24)
    async def daily_check(self):
        """Check and announce birthdays once per day."""
        try:
            today = date.today()
            await self._announce_birthdays(today)
        except Exception as e:
            print(f"[Birthday] Error in daily check: {e}")

    @daily_check.before_loop
    async def before_daily_check(self):
        await self.bot.wait_until_ready()

    async def cog_load(self):
        self.bot.tree.add_command(self.birthday)
        print("[Birthday] Slash command group registered.")
        self.daily_check.start()
        print("[Birthday] Daily announcement task started.")

    async def cog_unload(self):
        self.daily_check.cancel()


async def setup(bot: commands.Bot):
    await bot.add_cog(BirthdayCog(bot))
