import json
import os
import datetime
from datetime import date, timedelta
import discord
from discord.ext import commands, tasks
from discord import app_commands


DATA_FILE = os.getenv("BIRTHDAY_DATA_PATH", "data/birthdays.json")


class BirthdayCog(commands.Cog):
    """Birthday system with slash commands (flattened to avoid registration issues)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data: dict = {}
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
        if not date_str:
            return None
        s = date_str.strip()
        full_formats = ["%Y-%m-%d", "%d-%m-%Y", "%m-%d-%Y", "%d/%m/%Y", "%m/%d/%Y"]
        for fmt in full_formats:
            try:
                dt = datetime.datetime.strptime(s, fmt)
                return dt.month, dt.day
            except ValueError:
                continue
        md_formats = ["%d-%m", "%d/%m", "%m-%d", "%m/%d"]
        for fmt in md_formats:
            try:
                dt = datetime.datetime.strptime(s, fmt)
                return dt.month, dt.day
            except ValueError:
                continue
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
        try:
            this_year = date(today.year, month, day)
        except ValueError:
            this_year = date(today.year, month, min(day, 28))
        if this_year < today:
            try:
                next_bd = date(today.year + 1, month, day)
            except ValueError:
                next_bd = date(today.year + 1, month, min(day, 28))
        else:
            next_bd = this_year
        return (next_bd - today).days, next_bd

    async def _announce_birthdays(self, check_date: date):
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
                try:
                    await channel.send("🎉 **Happy Birthday today!** " + ", ".join(celebrants))
                except Exception:
                    pass

    # ==================== FLATTENED SLASH COMMANDS ====================

    @app_commands.command(name="birthday-set", description="Set your own birthday")
    @app_commands.describe(date="Date (e.g. 25-12, December 25)", year="Birth year (optional)")
    async def birthday_set(self, interaction: discord.Interaction, date: str, year: app_commands.Range[int, 1900, 2026] = None):
        if not interaction.guild:
            await interaction.response.send_message("Only usable in a server.", ephemeral=True)
            return
        parsed = self._parse_date(date)
        if not parsed:
            await interaction.response.send_message("Could not parse date.", ephemeral=True)
            return
        month, day = parsed
        gdata = self._get_guild_data(interaction.guild.id)
        gdata[str(interaction.user.id)] = {"month": month, "day": day, "year": year}
        self._save_data()
        await interaction.response.send_message(f"✅ Birthday set to {day:02d}-{month:02d}.", ephemeral=True)

    @app_commands.command(name="birthday-setfor", description="Set birthday for another member (Admin)")
    @app_commands.describe(user="Member", date="Date", year="Year (optional)")
    @app_commands.default_permissions(manage_guild=True)
    async def birthday_setfor(self, interaction: discord.Interaction, user: discord.Member, date: str, year: app_commands.Range[int, 1900, 2026] = None):
        if not interaction.guild:
            await interaction.response.send_message("Only usable in a server.", ephemeral=True)
            return
        parsed = self._parse_date(date)
        if not parsed:
            await interaction.response.send_message("Could not parse date.", ephemeral=True)
            return
        month, day = parsed
        gdata = self._get_guild_data(interaction.guild.id)
        gdata[str(user.id)] = {"month": month, "day": day, "year": year}
        self._save_data()
        await interaction.response.send_message(f"✅ Birthday for {user.mention} set.", ephemeral=True)

    @app_commands.command(name="birthday-remove", description="Remove a birthday")
    @app_commands.describe(user="Leave empty for yourself")
    async def birthday_remove(self, interaction: discord.Interaction, user: discord.Member = None):
        if not interaction.guild:
            await interaction.response.send_message("Only usable in a server.", ephemeral=True)
            return
        target = user or interaction.user
        gdata = self._get_guild_data(interaction.guild.id)
        uid = str(target.id)
        if uid not in gdata or uid == "config":
            await interaction.response.send_message("No birthday found.", ephemeral=True)
            return
        if target.id != interaction.user.id and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("No permission.", ephemeral=True)
            return
        del gdata[uid]
        self._save_data()
        await interaction.response.send_message(f"✅ Removed birthday for {target.mention}.", ephemeral=True)

    @app_commands.command(name="birthday-list", description="Show upcoming birthdays")
    async def birthday_list(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Only usable in a server.", ephemeral=True)
            return
        gdata = self._get_guild_data(interaction.guild.id)
        today = date.today()
        upcoming = []
        for uid_str, b in gdata.items():
            if uid_str == "config":
                continue
            try:
                days_until, next_date = self._get_days_until(b["month"], b["day"], today)
                member = interaction.guild.get_member(int(uid_str))
                name = member.display_name if member else uid_str
                upcoming.append((days_until, f"**{name}** — {next_date.strftime('%d %b')}"))
            except:
                continue
        if not upcoming:
            await interaction.response.send_message("No birthdays set.", ephemeral=True)
            return
        upcoming.sort()
        await interaction.response.send_message("\n".join([f"{d} days — {t}" for d, t in upcoming[:10]]), ephemeral=True)

    @app_commands.command(name="birthday-today", description="Who has birthday today?")
    async def birthday_today(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Only usable in a server.", ephemeral=True)
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
                    celebrants.append(member.mention)
        if not celebrants:
            await interaction.response.send_message("No birthdays today.", ephemeral=True)
            return
        await interaction.response.send_message("🎉 Happy Birthday: " + ", ".join(celebrants))

    @app_commands.command(name="birthday-channel", description="Set announcement channel (Admin)")
    @app_commands.default_permissions(manage_guild=True)
    async def birthday_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not interaction.guild:
            await interaction.response.send_message("Only usable in a server.", ephemeral=True)
            return
        gdata = self._get_guild_data(interaction.guild.id)
        if "config" not in gdata:
            gdata["config"] = {}
        gdata["config"]["announce_channel_id"] = channel.id
        self._save_data()
        await interaction.response.send_message(f"✅ Announcements will be sent to {channel.mention}.", ephemeral=True)

    # Background task
    @tasks.loop(hours=24)
    async def daily_check(self):
        try:
            await self._announce_birthdays(date.today())
        except Exception as e:
            print(f"[Birthday] Error: {e}")

    @daily_check.before_loop
    async def before_daily_check(self):
        await self.bot.wait_until_ready()

    async def cog_load(self):
        print("[Birthday] Cog loaded (flattened commands).")
        self.daily_check.start()

    async def cog_unload(self):
        self.daily_check.cancel()


async def setup(bot: commands.Bot):
    await bot.add_cog(BirthdayCog(bot))
