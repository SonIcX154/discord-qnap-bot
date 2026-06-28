import json
import os
import datetime
import random
import asyncio
from datetime import date, time, timedelta
import discord
from discord.ext import commands, tasks
from discord import app_commands


DATA_FILE = os.getenv("BIRTHDAY_DATA_PATH", "data/birthdays.json")
ANNOUNCE_HOUR = int(os.getenv("BIRTHDAY_ANNOUNCE_HOUR", "0"))
ANNOUNCE_MINUTE = int(os.getenv("BIRTHDAY_ANNOUNCE_MINUTE", "0"))


class BirthdayCog(commands.Cog):
    """Birthday system with slash commands (German parameters)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data: dict = {}
        self._load_data()
        self._last_announce_date = None   # Prevent duplicate runs in the same minute

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
        # German months
        s_lower = s.lower()
        german_months = {
            "januar": 1, "jänner": 1, "februar": 2, "märz": 3, "maerz": 3,
            "april": 4, "mai": 5, "juni": 6, "juli": 7, "august": 8,
            "september": 9, "oktober": 10, "november": 11, "dezember": 12
        }
        for name, num in german_months.items():
            if name in s_lower:
                import re
                match = re.search(r"(\d{1,2})", s)
                if match:
                    day = int(match.group(1))
                    if 1 <= day <= 31:
                        return num, day
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

    async def _get_member_name(self, guild: discord.Guild, user_id: int) -> str:
        member = guild.get_member(user_id)
        if member:
            return member.display_name
        try:
            member = await guild.fetch_member(user_id)
            return member.display_name
        except discord.NotFound:
            return str(user_id)
        except Exception:
            return str(user_id)

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
                print(f"[Birthday] Configured announce channel {channel_id} is not a TextChannel in guild {guild.id}")
                continue

            celebrants = []
            for uid_str, b in gdata.items():
                if uid_str == "config":
                    continue
                try:
                    if b.get("month") == check_date.month and b.get("day") == check_date.day:
                        # Use real Discord mention for pings
                        age_str = ""
                        if b.get("year"):
                            try:
                                age = check_date.year - int(b["year"])
                                if age > 0:
                                    age_str = f" (turns {age}!)\u2728"
                            except:
                                pass
                        celebrants.append(f"<@{uid_str}>{age_str}")
                except Exception:
                    continue

            if celebrants:
                if len(celebrants) == 1:
                    msg = f"## \ud83c\udf89Alles Gute zum Geburtstag, {celebrants[0]}!"
                elif len(celebrants) == 2:
                    msg = f"## \ud83c\udf89Alles Gute zum Geburtstag {celebrants[0]} und {celebrants[1]}!"
                else:
                    msg = f"## \ud83c\udf89Alles Gute zum Geburtstag {', '.join(celebrants[:-1])} und {celebrants[-1]}!"
                try:
                    await channel.send(msg)
                    print(f"[Birthday] ✅ Sent announcement to #{channel.name} ({channel.id}) in guild {guild.id} for {len(celebrants)} celebrant(s)")
                except Exception as e:
                    print(f"[Birthday] ❌ Failed to send announcement in guild {guild.id}: {e}")
            else:
                print(f"[Birthday] No birthdays today in guild {guild.id} (channel #{channel.name} is configured)")

    # ==================== SLASH COMMANDS (German parameters) ====================

    @app_commands.command(name="geburtstag-setzen", description="Lege deinen Geburtstag fest (Jahr optional)")
    @app_commands.describe(datum="Datum (z.B. 7 Januar oder 25-12)", jahr="Geburtsjahr (optional)")
    async def birthday_set(self, interaction: discord.Interaction, datum: str, jahr: app_commands.Range[int, 1900, 2026] = None):
        if not interaction.guild:
            await interaction.response.send_message("Nur in einem Server nutzbar.", ephemeral=True)
            return
        parsed = self._parse_date(datum)
        if not parsed:
            await interaction.response.send_message("Datum konnte nicht erkannt werden.", ephemeral=True)
            return
        month, day = parsed
        gdata = self._get_guild_data(interaction.guild.id)
        gdata[str(interaction.user.id)] = {"month": month, "day": day, "year": jahr}
        self._save_data()
        await interaction.response.send_message(f"✅ Dein Geburtstag wurde auf den {day:02d}.{month:02d}. gesetzt.")

    @app_commands.command(name="birthday-setfor", description="Geburtstag für ein anderes Mitglied setzen (Admin)")
    @app_commands.describe(benutzer="Mitglied", datum="Datum", jahr="Geburtsjahr (optional)")
    @app_commands.default_permissions(manage_guild=True)
    async def birthday_setfor(self, interaction: discord.Interaction, benutzer: discord.Member, datum: str, jahr: app_commands.Range[int, 1900, 2026] = None):
        if not interaction.guild:
            await interaction.response.send_message("Nur in einem Server nutzbar.", ephemeral=True)
            return
        parsed = self._parse_date(datum)
        if not parsed:
            await interaction.response.send_message("Datum konnte nicht erkannt werden.", ephemeral=True)
            return
        month, day = parsed
        gdata = self._get_guild_data(interaction.guild.id)
        gdata[str(benutzer.id)] = {"month": month, "day": day, "year": jahr}
        self._save_data()
        await interaction.response.send_message(f"✅ Geburtstag für {benutzer.mention} wurde gesetzt.", ephemeral=True)

    @app_commands.command(name="geburtstag-entfernen", description="Deinen Geburtstag entfernen oder den eines anderen Mitglieds (Admin)")
    @app_commands.describe(benutzer="Leer lassen, um deinen eigenen zu entfernen")
    async def birthday_remove(self, interaction: discord.Interaction, benutzer: discord.Member = None):
        if not interaction.guild:
            await interaction.response.send_message("Nur in einem Server nutzbar.", ephemeral=True)
            return
        target = benutzer or interaction.user
        gdata = self._get_guild_data(interaction.guild.id)
        uid = str(target.id)
        if uid not in gdata or uid == "config":
            await interaction.response.send_message("Kein Geburtstag gefunden.", ephemeral=True)
            return
        if target.id != interaction.user.id and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Keine Berechtigung.", ephemeral=True)
            return
        del gdata[uid]
        self._save_data()
        await interaction.response.send_message(f"✅ Geburtstag von {target.mention} wurde entfernt.", ephemeral=True)

    @app_commands.command(name="geburtstags-liste", description="Kommende Geburtstage anzeigen")
    async def birthday_list(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Nur in einem Server nutzbar.", ephemeral=True)
            return
        gdata = self._get_guild_data(interaction.guild.id)
        today = date.today()
        upcoming = []
        for uid_str, b in gdata.items():
            if uid_str == "config":
                continue
            try:
                name = await self._get_member_name(interaction.guild, int(uid_str))
                days_until, next_date = self._get_days_until(b["month"], b["day"], today)
                upcoming.append((days_until, f"**{name}** — {next_date.strftime('%d.%m.')}"))
            except:
                continue
        if not upcoming:
            await interaction.response.send_message("Noch keine Geburtstage eingetragen.")
            return
        upcoming.sort()
        await interaction.response.send_message("\n".join([f"{d} Tage — {t}" for d, t in upcoming[:10]]))

    @app_commands.command(name="geburtstag-heute", description="Wer hat heute Geburtstag?")
    async def birthday_today(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Nur in einem Server nutzbar.", ephemeral=True)
            return
        gdata = self._get_guild_data(interaction.guild.id)
        today = date.today()
        celebrants = []
        for uid_str, b in gdata.items():
            if uid_str == "config":
                continue
            try:
                name = await self._get_member_name(interaction.guild, int(uid_str))
                if b.get("month") == today.month and b.get("day") == today.day:
                    age_str = ""
                    if b.get("year"):
                        try:
                            age = today.year - int(b["year"])
                            if age > 0:
                                age_str = f" (turns {age}!)\u2728"
                        except:
                            pass
                    celebrants.append(f"@{name}{age_str}")
            except:
                continue
        if not celebrants:
            await interaction.response.send_message("Heute hat niemand Geburtstag")
            return
        if len(celebrants) == 1:
            await interaction.response.send_message(f"🎉 Heute hat {celebrants[0]} Geburtstag")
        elif len(celebrants) == 2:
            await interaction.response.send_message(f"🎉 Heute haben {celebrants[0]} und {celebrants[1]} Geburtstag")
        else:
            await interaction.response.send_message(f"🎉 Heute haben {', '.join(celebrants[:-1])} und {celebrants[-1]} Geburtstag")

    @app_commands.command(name="birthday-channel", description="Ankündigungskanal festlegen (Admin)")
    @app_commands.default_permissions(manage_guild=True)
    async def birthday_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not interaction.guild:
            await interaction.response.send_message("Nur in einem Server nutzbar.", ephemeral=True)
            return
        gdata = self._get_guild_data(interaction.guild.id)
        if "config" not in gdata:
            gdata["config"] = {}
        gdata["config"]["announce_channel_id"] = channel.id
        self._save_data()
        print(f"[Birthday] Announcement channel set to #{channel.name} ({channel.id}) for guild {interaction.guild.id} by {interaction.user}")
        await interaction.response.send_message(f"✅ Ankündigungen werden jetzt in {channel.mention} gesendet.", ephemeral=True)

    @app_commands.command(name="test-birthday-messages", description="Testet die automatischen Geburtstagsnachrichten mit echten Pings (Admin only)")
    @app_commands.default_permissions(manage_guild=True)
    async def test_birthday_messages(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        members = [m for m in interaction.guild.members if not m.bot]
        if len(members) < 3:
            await interaction.followup.send("Nicht genug Member auf dem Server (mind. 3 benötigt).", ephemeral=True)
            return

        random.shuffle(members)

        for count in [1, 2, 3]:
            selected = members[:count]
            celebrants = []
            for m in selected:
                age_str = ""
                celebrants.append(f"<@{m.id}>{age_str}")

            if len(celebrants) == 1:
                msg = f"## \ud83c\udf89Alles Gute zum Geburtstag, {celebrants[0]}!"
            elif len(celebrants) == 2:
                msg = f"## \ud83c\udf89Alles Gute zum Geburtstag {celebrants[0]} und {celebrants[1]}!"
            else:
                msg = f"## \ud83c\udf89Alles Gute zum Geburtstag {', '.join(celebrants[:-1])} und {celebrants[-1]}!"

            await interaction.channel.send(msg)
            await asyncio.sleep(1.2)

        await interaction.followup.send("✅ Test-Nachrichten (mit echten Pings) wurden in den Kanal gesendet.", ephemeral=True)

    @app_commands.command(name="force-birthday-check", description="Manually trigger today's birthday announcement check now (Admin only)")
    @app_commands.default_permissions(manage_guild=True)
    async def force_birthday_check(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        print(f"[Birthday] ⚙️ Manual force check triggered by {interaction.user} in guild {interaction.guild.id if interaction.guild else 'DM'}")
        try:
            await self._announce_birthdays(date.today())
            await interaction.followup.send("✅ Force birthday check completed. Check the logs above for details (sent / not sent / errors).", ephemeral=True)
        except Exception as e:
            print(f"[Birthday] Error during force check: {e}")
            await interaction.followup.send(f"❌ Error during force check: {e}", ephemeral=True)

    # ==================== RELIABLE DAILY SCHEDULER ====================

    @tasks.loop(minutes=1)
    async def daily_check(self):
        """Checks every minute whether it's time to send birthday announcements."""
        now = datetime.datetime.now()
        current_date = now.date()

        # Only run if current time matches configured time (and we haven't already run today)
        if (now.hour == ANNOUNCE_HOUR and now.minute == ANNOUNCE_MINUTE and
                self._last_announce_date != current_date):

            print(f"[Birthday] Running daily birthday check for {current_date} (configured time {ANNOUNCE_HOUR:02d}:{ANNOUNCE_MINUTE:02d})")
            self._last_announce_date = current_date

            try:
                await self._announce_birthdays(current_date)
            except Exception as e:
                print(f"[Birthday] Error in daily check: {e}")

    @daily_check.before_loop
    async def before_daily_check(self):
        await self.bot.wait_until_ready()

    async def cog_load(self):
        print(f"[Birthday] Cog loaded. Daily announcements configured for {ANNOUNCE_HOUR:02d}:{ANNOUNCE_MINUTE:02d} every day.")

        # Debug logs
        print(f"[DEBUG] Guilds bot sees: {[(g.id, g.name) for g in self.bot.guilds]}")
        print(f"[DEBUG] self.data keys: {list(self.data.keys())}")

        # Log configured announcement channels per guild
        for guild in self.bot.guilds:
            gdata = self.data.get(str(guild.id), {})
            config = gdata.get("config", {})
            channel_id = config.get("announce_channel_id")
            if channel_id:
                channel = guild.get_channel(channel_id)
                if channel:
                    print(f"[Birthday] Announcement channel for {guild.name}: #{channel.name} ({channel.id})")
                else:
                    print(f"[Birthday] Announcement channel set for {guild.name} but channel {channel_id} not found")

        self.daily_check.start()

    async def cog_unload(self):
        self.daily_check.cancel()


async def setup(bot: commands.Bot):
    await bot.add_cog(BirthdayCog(bot))
