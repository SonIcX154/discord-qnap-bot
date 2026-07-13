import os
import time
import random
import asyncio
import aiosqlite
import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional, List, Tuple


# ====================== CONFIG ======================
ECONOMY_DB_PATH = os.getenv("ECONOMY_DATA_PATH", "data/economy.db")
DEFAULT_CURRENCY = "Coins"

# Earning rates (balanced so chat and voice give similar income per active time)
CHAT_COINS = 3
CHAT_COOLDOWN_SECONDS = 45
VOICE_COINS_PER_MINUTE = 3
DAILY_COINS_MIN = 80
DAILY_COINS_MAX = 120
DAILY_COOLDOWN_SECONDS = 86400  # 24 hours


class EconomyCog(commands.Cog):
    """Economy & Gambling system with fictional (renamable) currency.
    
    Features:
    - Global user balances (one DB per bot instance)
    - Earn via chat + voice (only active users: not deaf/mute)
    - /daily, /balance, /leaderboard
    - Coinflip + Slots
    - Admin commands (/economy give/take/set)
    - Currency name changeable by admins
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_path = ECONOMY_DB_PATH
        self._voice_task: Optional[asyncio.Task] = None

    async def cog_load(self):
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        await self._init_db()
        self._voice_task = asyncio.create_task(self._voice_earnings_loop())
        print(f"[Economy] Cog loaded. DB: {self.db_path}")

    async def cog_unload(self):
        if self._voice_task and not self._voice_task.done():
            self._voice_task.cancel()
            try:
                await self._voice_task
            except asyncio.CancelledError:
                pass

    async def _init_db(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    balance INTEGER NOT NULL DEFAULT 0,
                    last_daily INTEGER,
                    last_chat_earn INTEGER
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            await db.execute("""
                INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)
            """, ("currency_name", DEFAULT_CURRENCY))
            await db.commit()

    # ==================== CURRENCY (renamable) ====================

    async def get_currency_name(self) -> str:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT value FROM config WHERE key = ?", ("currency_name",)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else DEFAULT_CURRENCY

    async def set_currency_name(self, name: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO config (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = ?
            """, ("currency_name", name, name))
            await db.commit()

    # ==================== BALANCE HELPERS ====================

    async def get_balance(self, user_id: int) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT balance FROM users WHERE user_id = ?", (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    async def add_coins(self, user_id: int, amount: int) -> int:
        if amount <= 0:
            return await self.get_balance(user_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO users (user_id, balance)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET balance = balance + ?
            """, (user_id, amount, amount))
            await db.commit()
            return await self.get_balance(user_id)

    async def remove_coins(self, user_id: int, amount: int) -> bool:
        if amount <= 0:
            return True
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT balance FROM users WHERE user_id = ?", (user_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    current = row[0] if row else 0

                if current < amount:
                    await db.rollback()
                    return False

                await db.execute(
                    "UPDATE users SET balance = balance - ? WHERE user_id = ?",
                    (amount, user_id)
                )
                await db.commit()
                return True
            except Exception:
                await db.rollback()
                raise

    async def get_last_daily(self, user_id: int) -> Optional[int]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT last_daily FROM users WHERE user_id = ?", (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row and row[0] is not None else None

    async def set_last_daily(self, user_id: int, timestamp: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO users (user_id, last_daily)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET last_daily = ?
            """, (user_id, timestamp, timestamp))
            await db.commit()

    async def get_last_chat_earn(self, user_id: int) -> Optional[int]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT last_chat_earn FROM users WHERE user_id = ?", (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row and row[0] is not None else None

    async def set_last_chat_earn(self, user_id: int, timestamp: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO users (user_id, last_chat_earn)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET last_chat_earn = ?
            """, (user_id, timestamp, timestamp))
            await db.commit()

    # ==================== EARNING ====================

    async def can_earn_chat(self, user_id: int) -> bool:
        last = await self.get_last_chat_earn(user_id)
        if last is None:
            return True
        return (int(time.time()) - last) >= CHAT_COOLDOWN_SECONDS

    async def claim_chat_earn(self, user_id: int) -> int:
        if not await self.can_earn_chat(user_id):
            return 0
        amount = CHAT_COINS
        await self.add_coins(user_id, amount)
        await self.set_last_chat_earn(user_id, int(time.time()))
        return amount

    async def can_claim_daily(self, user_id: int) -> bool:
        last = await self.get_last_daily(user_id)
        if last is None:
            return True
        return (int(time.time()) - last) >= DAILY_COOLDOWN_SECONDS

    async def claim_daily(self, user_id: int) -> int:
        if not await self.can_claim_daily(user_id):
            return 0
        amount = random.randint(DAILY_COINS_MIN, DAILY_COINS_MAX)
        new_balance = await self.add_coins(user_id, amount)
        await self.set_last_daily(user_id, int(time.time()))
        return amount

    # ==================== VOICE EARNING (only active users) ====================

    async def _voice_earnings_loop(self):
        await self.bot.wait_until_ready()
        print("[Economy] Voice earnings task started (only active users).")

        while True:
            try:
                for guild in self.bot.guilds:
                    for vc in guild.voice_channels:
                        for member in vc.members:
                            if member.bot:
                                continue
                            voice_state = member.voice
                            if voice_state and not voice_state.self_deaf and not voice_state.self_mute:
                                await self.add_coins(member.id, VOICE_COINS_PER_MINUTE)
            except Exception as e:
                print(f"[Economy] Voice earnings error: {e}")
            await asyncio.sleep(60)

    # ==================== LISTENERS ====================

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        user_id = message.author.id
        await self.claim_chat_earn(user_id)  # silent

    # ==================== ADMIN: SET CURRENCY ====================

    @app_commands.command(name="set-currency", description="Ändere den Namen der Währung (Admin)")
    @app_commands.describe(name="Neuer Name der Währung (z.B. Q-Coins, Credits, Tokens)")
    @app_commands.default_permissions(manage_guild=True)
    async def set_currency(self, interaction: discord.Interaction, name: str):
        if len(name) > 32:
            await interaction.response.send_message("❌ Name zu lang (max 32 Zeichen).", ephemeral=True)
            return

        await self.set_currency_name(name.strip())
        currency = await self.get_currency_name()

        embed = discord.Embed(
            title="✅ Währung geändert",
            description=f"Die Währung heißt jetzt **{currency}**.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ==================== ADMIN COMMAND GROUP ====================

    @app_commands.group(name="economy", description="Admin-Befehle für das Economy-System")
    @app_commands.default_permissions(manage_guild=True)
    async def economy_group(self, interaction: discord.Interaction):
        """Base group for economy admin commands."""
        pass

    @economy_group.command(name="give", description="Gib einem User Coins (Admin)")
    @app_commands.describe(user="User der Coins bekommen soll", amount="Anzahl der Coins")
    async def economy_give(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, None]):
        currency = await self.get_currency_name()
        old_balance = await self.get_balance(user.id)
        new_balance = await self.add_coins(user.id, amount)

        embed = discord.Embed(title="✅ Coins gegeben", color=discord.Color.green())
        embed.add_field(name="User", value=user.mention, inline=True)
        embed.add_field(name="Betrag", value=f"+{amount:,} {currency}", inline=True)
        embed.add_field(name="Neuer Kontostand", value=f"**{new_balance:,}** {currency} (vorher: {old_balance:,})", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @economy_group.command(name="take", description="Nimm einem User Coins weg (Admin)")
    @app_commands.describe(user="User von dem Coins abgezogen werden sollen", amount="Anzahl der Coins")
    async def economy_take(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, None]):
        currency = await self.get_currency_name()
        old_balance = await self.get_balance(user.id)

        success = await self.remove_coins(user.id, amount)
        if not success:
            await interaction.response.send_message(
                f"❌ {user.mention} hat nicht genug {currency} (hat nur {old_balance:,}).",
                ephemeral=True
            )
            return

        new_balance = await self.get_balance(user.id)
        embed = discord.Embed(title="✅ Coins abgezogen", color=discord.Color.orange())
        embed.add_field(name="User", value=user.mention, inline=True)
        embed.add_field(name="Betrag", value=f"-{amount:,} {currency}", inline=True)
        embed.add_field(name="Neuer Kontostand", value=f"**{new_balance:,}** {currency} (vorher: {old_balance:,})", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @economy_group.command(name="set", description="Setze den exakten Kontostand eines Users (Admin)")
    @app_commands.describe(user="User", amount="Neuer exakter Kontostand")
    async def economy_set(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 0, None]):
        currency = await self.get_currency_name()
        old_balance = await self.get_balance(user.id)

        # Set balance directly
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO users (user_id, balance) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET balance = ?
            """, (user.id, amount, amount))
            await db.commit()

        embed = discord.Embed(title="✅ Kontostand gesetzt", color=discord.Color.blue())
        embed.add_field(name="User", value=user.mention, inline=True)
        embed.add_field(name="Alter Stand", value=f"{old_balance:,} {currency}", inline=True)
        embed.add_field(name="Neuer Stand", value=f"**{amount:,}** {currency}", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ==================== GAMBLING: COINFLIP ====================

    @app_commands.command(name="coinflip", description="Setze auf einen Münzwurf (50/50)")
    @app_commands.describe(bet="Einsatz in Währung (Min. 10)")
    @app_commands.checks.cooldown(1, 3.0, key=lambda interaction: interaction.user.id)
    async def coinflip(self, interaction: discord.Interaction, bet: app_commands.Range[int, 10, None]):
        user_id = interaction.user.id
        currency = await self.get_currency_name()

        current_balance = await self.get_balance(user_id)
        if current_balance < bet:
            await interaction.response.send_message(
                f"❌ Nicht genug {currency}! Dein Kontostand: **{current_balance:,}** {currency}.",
                ephemeral=True
            )
            return

        if not await self.remove_coins(user_id, bet):
            await interaction.response.send_message("❌ Fehler beim Abziehen des Einsatzes.", ephemeral=True)
            return

        result = random.choice(["Kopf", "Zahl"])
        won = random.random() < 0.5

        if won:
            new_balance = await self.add_coins(user_id, bet)
            embed = discord.Embed(title="🪙 Coinflip - Gewonnen!", color=discord.Color.green())
            embed.description = f"Die Münze ist auf **{result}** gelandet."
            embed.add_field(name="Einsatz", value=f"{bet:,} {currency}", inline=True)
            embed.add_field(name="Gewinn", value=f"+{bet:,} {currency}", inline=True)
            embed.add_field(name="Neuer Kontostand", value=f"**{new_balance:,}** {currency}", inline=False)
        else:
            new_balance = await self.get_balance(user_id)
            embed = discord.Embed(title="🪙 Coinflip - Verloren", color=discord.Color.red())
            embed.description = f"Die Münze ist auf **{result}** gelandet."
            embed.add_field(name="Einsatz", value=f"{bet:,} {currency}", inline=True)
            embed.add_field(name="Verlust", value=f"-{bet:,} {currency}", inline=True)
            embed.add_field(name="Neuer Kontostand", value=f"**{new_balance:,}** {currency}", inline=False)

        embed.set_footer(text=f"Gespielt von {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)

    @coinflip.error
    async def coinflip_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"⏳ Warte noch **{error.retry_after:.1f}s**.", ephemeral=True
            )
        else:
            raise error

    # ==================== GAMBLING: SLOTS ====================

    SLOT_SYMBOLS: List[str] = ["🍒", "🍋", "🔔", "⭐", "7️⃣", "💎"]

    def _roll_slots(self) -> Tuple[List[str], int, str]:
        reels = [random.choice(self.SLOT_SYMBOLS) for _ in range(3)]

        if reels[0] == reels[1] == reels[2]:
            if reels[0] == "💎":
                multiplier = 10
                win_text = "JACKPOT! 10x 💎"
            elif reels[0] == "7️⃣":
                multiplier = 7
                win_text = "7x Siebenen!"
            elif reels[0] == "⭐":
                multiplier = 5
                win_text = "5x Sterne!"
            elif reels[0] == "🔔":
                multiplier = 4
                win_text = "4x Glocken!"
            else:
                multiplier = 3
                win_text = "3x Gleich!"
        elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
            multiplier = 2
            win_text = "2x Gleich!"
        else:
            multiplier = 0
            win_text = "Nichts..."

        return reels, multiplier, win_text

    @app_commands.command(name="slots", description="Spiele Slots mit 3 Walzen")
    @app_commands.describe(bet="Einsatz (Min. 10)")
    @app_commands.checks.cooldown(1, 3.0, key=lambda interaction: interaction.user.id)
    async def slots(self, interaction: discord.Interaction, bet: app_commands.Range[int, 10, None]):
        user_id = interaction.user.id
        currency = await self.get_currency_name()

        current = await self.get_balance(user_id)
        if current < bet:
            await interaction.response.send_message(
                f"❌ Nicht genug {currency}! Du hast **{current:,}** {currency}.",
                ephemeral=True
            )
            return

        if not await self.remove_coins(user_id, bet):
            await interaction.response.send_message("❌ Fehler beim Einsatz.", ephemeral=True)
            return

        reels, multiplier, win_text = self._roll_slots()
        winnings = int(bet * multiplier)

        if multiplier > 0:
            new_balance = await self.add_coins(user.id, winnings)
            color = discord.Color.green()
            title = "🎰 SLOTS - GEWONNEN!"
        else:
            new_balance = await self.get_balance(user.id)
            color = discord.Color.red()
            title = "🎰 SLOTS - Verloren"

        embed = discord.Embed(title=title, color=color)
        embed.description = f"**{' | '.join(reels)}**"
        embed.add_field(name="Einsatz", value=f"{bet:,} {currency}", inline=True)
        if multiplier > 0:
            embed.add_field(name="Gewinn", value=f"+{winnings:,} {currency} ({win_text})", inline=True)
        else:
            embed.add_field(name="Ergebnis", value=win_text, inline=True)
        embed.add_field(name="Neuer Kontostand", value=f"**{new_balance:,}** {currency}", inline=False)
        embed.set_footer(text=f"Gespielt von {interaction.user.display_name} • RTP ~92%")

        await interaction.response.send_message(embed=embed)

    @slots.error
    async def slots_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(f"⏳ Warte **{error.retry_after:.1f}s**.", ephemeral=True)
        else:
            raise error

    # ==================== LEADERBOARD ====================

    async def get_top_users(self, limit: int = 10) -> List[Tuple[int, int]]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT ?", (limit,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [(row[0], row[1]) for row in rows]

    @app_commands.command(name="leaderboard", description="Zeigt die Top 10 reichsten User an")
    async def leaderboard(self, interaction: discord.Interaction):
        currency = await self.get_currency_name()
        top_users = await self.get_top_users(10)

        if not top_users:
            await interaction.response.send_message("Noch keine Daten im Leaderboard.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"🏆 {currency} Leaderboard (Top 10)",
            color=discord.Color.gold()
        )

        lines = []
        for i, (user_id, balance) in enumerate(top_users, 1):
            member = interaction.guild.get_member(user_id) if interaction.guild else None
            if member:
                name = member.display_name
            else:
                try:
                    if interaction.guild:
                        member = await interaction.guild.fetch_member(user_id)
                        name = member.display_name
                    else:
                        name = f"User {user_id}"
                except:
                    name = f"User {user_id}"

            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            lines.append(f"{medal} **{name}** — {balance:,} {currency}")

        embed.description = "\n".join(lines)
        embed.set_footer(text="Global pro Bot-Instanz • /balance für deinen Stand")
        await interaction.response.send_message(embed=embed)

    # ==================== USER COMMANDS ====================

    @app_commands.command(name="balance", description="Zeigt deinen aktuellen Kontostand")
    @app_commands.describe(user="Optional: anderer User")
    async def balance(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
        target = user or interaction.user
        bal = await self.get_balance(target.id)
        currency = await self.get_currency_name()

        embed = discord.Embed(
            title=f"💰 {target.display_name}'s {currency}",
            description=f"**{bal:,}** {currency}",
            color=discord.Color.gold()
        )
        embed.set_footer(text="/daily • /coinflip • /slots • /leaderboard")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="daily", description="Täglicher Bonus (einmal alle 24h)")
    async def daily(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        currency = await self.get_currency_name()

        if not await self.can_claim_daily(user_id):
            last = await self.get_last_daily(user_id)
            remaining = DAILY_COOLDOWN_SECONDS - (int(time.time()) - last) if last else 0
            hours = remaining // 3600
            minutes = (remaining % 3600) // 60
            await interaction.response.send_message(
                f"⏳ Daily schon geholt. Nächster in **{hours}h {minutes}m**.",
                ephemeral=True
            )
            return

        amount = await self.claim_daily(user_id)
        new_balance = await self.get_balance(user_id)

        embed = discord.Embed(title="🎁 Täglicher Bonus", color=discord.Color.green())
        embed.description = f"Du hast **{amount} {currency}** erhalten!"
        embed.add_field(name="Neuer Kontostand", value=f"**{new_balance:,}** {currency}", inline=False)
        embed.set_footer(text="Bis morgen! 💰")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCog(bot))
