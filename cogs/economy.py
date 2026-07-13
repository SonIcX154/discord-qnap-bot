import os
import time
import random
import asyncio
import aiosqlite
import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional


# ====================== CONFIG ======================
ECONOMY_DB_PATH = os.getenv("ECONOMY_DATA_PATH", "data/economy.db")

# Earning rates (balanced so chat and voice give similar income per active time)
CHAT_COINS = 3
CHAT_COOLDOWN_SECONDS = 45
VOICE_COINS_PER_MINUTE = 3
DAILY_COINS_MIN = 80
DAILY_COINS_MAX = 120
DAILY_COOLDOWN_SECONDS = 86400  # 24 hours


class EconomyCog(commands.Cog):
    """Economy & Gambling system with fictional Coins.
    
    Features:
    - Global user balances (one DB per bot instance / per guild container)
    - Earn coins via chat (cooldown) and voice activity
    - /daily command
    - Coinflip gambling game
    - Prepared for Slots, Roulette
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_path = ECONOMY_DB_PATH
        self._voice_task: Optional[asyncio.Task] = None

    async def cog_load(self):
        """Initialize database and start background tasks."""
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
        """Create tables if they don't exist."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    balance INTEGER NOT NULL DEFAULT 0,
                    last_daily INTEGER,
                    last_chat_earn INTEGER
                )
            """)
            await db.commit()

    # ==================== BALANCE HELPERS (with transactions) ====================

    async def get_balance(self, user_id: int) -> int:
        """Get current balance for a user."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT balance FROM users WHERE user_id = ?", (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    async def add_coins(self, user_id: int, amount: int) -> int:
        """Add coins to a user (creates user if not exists). Returns new balance."""
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
        """Try to remove coins atomically. Returns True if successful (had enough)."""
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

    # ==================== EARNING LOGIC ====================

    async def can_earn_chat(self, user_id: int) -> bool:
        last = await self.get_last_chat_earn(user_id)
        if last is None:
            return True
        return (int(time.time()) - last) >= CHAT_COOLDOWN_SECONDS

    async def claim_chat_earn(self, user_id: int) -> int:
        """Awards chat coins if cooldown passed. Returns amount awarded or 0."""
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
        """Claims daily bonus if ready. Returns amount or 0."""
        if not await self.can_claim_daily(user_id):
            return 0
        amount = random.randint(DAILY_COINS_MIN, DAILY_COINS_MAX)
        new_balance = await self.add_coins(user_id, amount)
        await self.set_last_daily(user_id, int(time.time()))
        return amount

    # ==================== BACKGROUND VOICE EARNING ====================

    async def _voice_earnings_loop(self):
        """Every minute, award coins to users currently in voice channels."""
        await self.bot.wait_until_ready()
        print("[Economy] Voice earnings task started.")

        while True:
            try:
                for guild in self.bot.guilds:
                    for vc in guild.voice_channels:
                        for member in vc.members:
                            if member.bot:
                                continue
                            # Award voice coins (simple: everyone in VC gets coins every minute)
                            await self.add_coins(member.id, VOICE_COINS_PER_MINUTE)
            except Exception as e:
                print(f"[Economy] Voice earnings error: {e}")

            await asyncio.sleep(60)

    # ==================== LISTENERS ====================

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Award coins for chatting (with cooldown)."""
        if message.author.bot:
            return
        if not message.guild:
            return  # Only earn in guilds

        user_id = message.author.id
        earned = await self.claim_chat_earn(user_id)
        # Silent earn - no spam in chat. User sees success via /balance
        if earned > 0:
            # Optional debug log (uncomment if needed)
            # print(f"[Economy] {message.author} earned {earned} coins from chat")
            pass

    # ==================== GAMBLING: COINFLIP ====================

    @app_commands.command(name="coinflip", description="Setze Coins auf einen Münzwurf (50/50 Chance)")
    @app_commands.describe(bet="Wie viele Coins willst du setzen? (Minimum 10)")
    @app_commands.checks.cooldown(1, 3.0, key=lambda interaction: interaction.user.id)
    async def coinflip(self, interaction: discord.Interaction, bet: app_commands.Range[int, 10, None]):
        user_id = interaction.user.id

        # Check if user has enough coins
        current_balance = await self.get_balance(user_id)
        if current_balance < bet:
            await interaction.response.send_message(
                f"❌ Du hast nicht genug Coins! Dein Kontostand: **{current_balance:,}** Coins.",
                ephemeral=True
            )
            return

        # Atomic remove bet
        success = await self.remove_coins(user_id, bet)
        if not success:
            await interaction.response.send_message(
                "❌ Konnte den Einsatz nicht abziehen. Bitte versuche es erneut.",
                ephemeral=True
            )
            return

        # 50/50 flip
        result = random.choice(["Kopf", "Zahl"])
        won = random.random() < 0.5  # True = win

        if won:
            # Win: get bet back + bet profit
            new_balance = await self.add_coins(user_id, bet)
            embed = discord.Embed(
                title="🪙 Coinflip - Gewonnen!",
                description=f"Die Münze ist auf **{result}** gelandet.",
                color=discord.Color.green()
            )
            embed.add_field(name="Einsatz", value=f"{bet:,} Coins", inline=True)
            embed.add_field(name="Gewinn", value=f"+{bet:,} Coins", inline=True)
            embed.add_field(name="Neuer Kontostand", value=f"**{new_balance:,}** Coins", inline=False)
        else:
            new_balance = await self.get_balance(user_id)
            embed = discord.Embed(
                title="🪙 Coinflip - Verloren",
                description=f"Die Münze ist auf **{result}** gelandet.",
                color=discord.Color.red()
            )
            embed.add_field(name="Einsatz", value=f"{bet:,} Coins", inline=True)
            embed.add_field(name="Verlust", value=f"-{bet:,} Coins", inline=True)
            embed.add_field(name="Neuer Kontostand", value=f"**{new_balance:,}** Coins", inline=False)

        embed.set_footer(text=f"Gespielt von {interaction.user.display_name} • 50/50 Chance")
        await interaction.response.send_message(embed=embed)

    @coinflip.error
    async def coinflip_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"⏳ Warte noch **{error.retry_after:.1f} Sekunden** bevor du wieder coinflipst.",
                ephemeral=True
            )
        else:
            # Let other errors propagate (or handle more specifically)
            raise error

    # ==================== SLASH COMMANDS ====================

    @app_commands.command(name="balance", description="Zeigt deinen aktuellen Coin-Bestand oder den eines anderen Users an")
    @app_commands.describe(user="Optional: User dessen Balance du sehen willst")
    async def balance(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
        target = user or interaction.user
        balance = await self.get_balance(target.id)

        embed = discord.Embed(
            title=f"💰 {target.display_name}'s Balance",
            description=f"**{balance:,}** Coins",
            color=discord.Color.gold()
        )
        embed.set_footer(text="Economy System • /daily für täglichen Bonus")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="daily", description="Hole deinen täglichen Coin-Bonus (einmal alle 24h)")
    async def daily(self, interaction: discord.Interaction):
        user_id = interaction.user.id

        if not await self.can_claim_daily(user_id):
            last = await self.get_last_daily(user_id)
            remaining = DAILY_COOLDOWN_SECONDS - (int(time.time()) - last) if last else 0
            hours = remaining // 3600
            minutes = (remaining % 3600) // 60
            await interaction.response.send_message(
                f"⏳ Du hast deinen Daily schon geholt. Nächster in **{hours}h {minutes}m**.",
                ephemeral=True
            )
            return

        amount = await self.claim_daily(user_id)
        new_balance = await self.get_balance(user_id)

        embed = discord.Embed(
            title="🎁 Täglicher Bonus",
            description=f"Du hast **{amount} Coins** erhalten!",
            color=discord.Color.green()
        )
        embed.add_field(name="Neuer Kontostand", value=f"**{new_balance:,}** Coins", inline=False)
        embed.set_footer(text="Komm morgen wieder für mehr! 💰")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCog(bot))
