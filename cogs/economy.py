from __future__ import annotations

import os
import time
import random
import asyncio
import aiosqlite
import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from utils.bet_mixin import BetAdjustableMixin
    from utils.replay_mixin import ReplayMixin

try:
    from utils.bet_mixin import BetAdjustableMixin
    from utils.replay_mixin import ReplayMixin
except ImportError:
    from ..utils.bet_mixin import BetAdjustableMixin
    from ..utils.replay_mixin import ReplayMixin


# ====================== CONFIG ======================
ECONOMY_DB_PATH = os.getenv("ECONOMY_DATA_PATH", "data/economy.db")
DEFAULT_CURRENCY = "Coins"
LEADERBOARD_PER_PAGE = 10

# Earning rates
CHAT_COINS = 3
CHAT_HOURLY_LIMIT = 180          # Max Coins pro Stunde durch Chatten

VOICE_COINS_PER_MINUTE = 2        # Reduziert auf 2 (nur bei ≥2 aktiven Personen)

DAILY_COINS_MIN = 80
DAILY_COINS_MAX = 150
DAILY_COOLDOWN_SECONDS = 24 * 60 * 60  # 24 hours


class LeaderboardView(discord.ui.View):
    """Interactive leaderboard with pagination and 'My Position' button."""

    def __init__(self, cog: "EconomyCog", interaction: discord.Interaction, currency: str) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.interaction = interaction
        self.currency = currency
        self.current_page: int = 1
        self.total_pages: int = 1
        self.highlight_user_id: int = interaction.user.id

    async def get_total_pages(self) -> int:
        total_users = await self.cog.get_total_users()
        return max(1, (total_users + LEADERBOARD_PER_PAGE - 1) // LEADERBOARD_PER_PAGE)

    async def update_embed(self) -> discord.Embed:
        self.total_pages = await self.get_total_pages()
        users = await self.cog.get_leaderboard_page(self.current_page, LEADERBOARD_PER_PAGE)

        embed = discord.Embed(
            title=f"🏆 {self.currency} Leaderboard (Seite {self.current_page}/{self.total_pages})",
            color=discord.Color.gold()
        )

        lines: list[str] = []
        start_rank = (self.current_page - 1) * LEADERBOARD_PER_PAGE + 1

        for i, (user_id, balance) in enumerate(users):
            rank = start_rank + i
            member = self.interaction.guild.get_member(user_id) if self.interaction.guild else None
            if member:
                name = member.display_name
            else:
                try:
                    if self.interaction.guild:
                        member = await self.interaction.guild.fetch_member(user_id)
                        name = member.display_name
                    else:
                        name = f"User {user_id}"
                except:
                    name = f"User {user_id}"

            if user_id == self.highlight_user_id:
                prefix = "➔ **"
                suffix = "** ⬅️"
            else:
                prefix = ""
                suffix = ""

            if rank == 1:
                medal = "🥇"
            elif rank == 2:
                medal = "🥈"
            elif rank == 3:
                medal = "🥉"
            else:
                medal = f"{rank}."

            lines.append(f"{medal} {prefix}{name}{suffix} — {balance:,} {self.currency}")

        embed.description = "\n".join(lines) if lines else "Keine Daten vorhanden."
        embed.set_footer(text="Global pro Bot-Instanz • Klicke auf 'Meine Position'")
        return embed

    @discord.ui.button(label="◀️ Zurück", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button[Any]) -> None:
        if self.current_page > 1:
            self.current_page -= 1
        embed = await self.update_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="▶️ Weiter", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button[Any]) -> None:
        total = await self.get_total_pages()
        if self.current_page < total:
            self.current_page += 1
        embed = await self.update_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="📍 Meine Position", style=discord.ButtonStyle.primary)
    async def my_position_button(self, interaction: discord.Interaction, button: discord.ui.Button[Any]) -> None:
        rank = await self.cog.get_user_rank(interaction.user.id)
        if rank is None:
            await interaction.response.send_message("Du hast noch keine Coins.", ephemeral=True)
            return

        new_page = (rank - 1) // LEADERBOARD_PER_PAGE + 1
        self.current_page = new_page
        self.highlight_user_id = interaction.user.id

        embed = await self.update_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]


class CoinflipView(BetAdjustableMixin, ReplayMixin, discord.ui.View):
    """Interactive Coinflip with bet adjustment and replay."""

    def __init__(self, economy_cog: "EconomyCog", interaction: discord.Interaction, bet: int, currency: str) -> None:
        BetAdjustableMixin.__init__(self, economy_cog, interaction.user.id, bet, currency)
        ReplayMixin.__init__(self, interaction.user.id)
        discord.ui.View.__init__(self, timeout=120)

    async def _do_replay(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="🪙 Coinflip - Wähle deine Seite",
            description=f"Du hast **{self.bet:,} {self.currency}** gesetzt.\n\nPasse deinen Einsatz an und wähle dann **Kopf** oder **Zahl**:",
            color=discord.Color.gold()
        )
        embed.set_footer(text="Timeout nach 2 Minuten")

        new_view = CoinflipView(self.economy, interaction, self.bet, self.currency)
        await interaction.response.edit_message(embed=embed, view=new_view)

    async def _resolve(self, interaction: discord.Interaction, choice: str) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]

        result = random.choice(["Kopf", "Zahl"])
        won = (choice == result)

        if won:
            new_balance = await self.economy.add_coins(self.user_id, self.bet)
            color = discord.Color.green()
            title = "🪙 Coinflip - Gewonnen!"
            win_text = f"+{self.bet:,} {self.currency}"
        else:
            new_balance = await self.economy.get_balance(self.user_id)
            color = discord.Color.red()
            title = "🪙 Coinflip - Verloren"
            win_text = f"-{self.bet:,} {self.currency}"

        embed = discord.Embed(title=title, color=color)
        embed.description = f"Die Münze ist auf **{result}** gelandet."
        embed.add_field(name="Deine Wahl", value=choice, inline=True)
        embed.add_field(name="Ergebnis", value=win_text, inline=True)
        embed.add_field(name="Neuer Kontostand", value=f"**{new_balance:,}** {self.currency}", inline=False)
        embed.set_footer(text=f"Gespielt von {interaction.user.display_name}")

        new_view = CoinflipView(self.economy, interaction, self.bet, self.currency)
        await interaction.response.edit_message(embed=embed, view=new_view)

    @discord.ui.button(label="🪙 Kopf", style=discord.ButtonStyle.primary, row=1)
    async def choose_kopf(self, interaction: discord.Interaction, button: discord.ui.Button[Any]) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Nur der Spieler darf wählen.", ephemeral=True)
            return
        await self._resolve(interaction, "Kopf")

    @discord.ui.button(label="🪙 Zahl", style=discord.ButtonStyle.primary, row=1)
    async def choose_zahl(self, interaction: discord.Interaction, button: discord.ui.Button[Any]) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Nur der Spieler darf wählen.", ephemeral=True)
            return
        await self._resolve(interaction, "Zahl")

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]


class EconomyCog(commands.Cog):
    """Core Economy system.
    
    Handles:
    - User balances (global)
    - Earning via chat (max 180 Coins/Stunde) and voice (2 Coins/Min bei ≥2 aktiven Personen)
    - Daily rewards
    - Leaderboard with interactive pagination
    - Admin commands
    - Currency name management
    - Coinflip, Slots, Roulette
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db_path = ECONOMY_DB_PATH
        self._voice_task: Optional[asyncio.Task[None]] = None

    async def cog_load(self) -> None:
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        await self._init_db()
        self._voice_task = asyncio.create_task(self._voice_earnings_loop())
        print(f"[Economy] Core Economy Cog loaded. DB: {self.db_path}")

    async def cog_unload(self) -> None:
        if self._voice_task and not self._voice_task.done():
            self._voice_task.cancel()
            try:
                await self._voice_task
            except asyncio.CancelledError:
                pass

    async def _init_db(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    balance INTEGER NOT NULL DEFAULT 0,
                    last_daily INTEGER
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

            # Neue Tabelle für stündliches Chat-Limit Tracking
            await db.execute("""
                CREATE TABLE IF NOT EXISTS chat_earnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    timestamp INTEGER NOT NULL
                )
            """)
            await db.commit()

    # ==================== PUBLIC METHODS (used by other cogs like Slots/Roulette) ====================

    async def get_currency_name(self) -> str:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT value FROM config WHERE key = ?", ("currency_name",)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else DEFAULT_CURRENCY

    async def set_currency_name(self, name: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO config (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = ?
            """, ("currency_name", name, name))
            await db.commit()

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

    async def set_last_daily(self, user_id: int, timestamp: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO users (user_id, last_daily)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET last_daily = ?
            """, (user_id, timestamp, timestamp))
            await db.commit()

    async def get_total_users(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM users") as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    async def get_leaderboard_page(self, page: int, per_page: int) -> list[tuple[int, int]]:
        offset = (page - 1) * per_page
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT ? OFFSET ?",
                (per_page, offset)
            ) as cursor:
                rows = await cursor.fetchall()
                return [(int(r[0]), int(r[1])) for r in rows]

    async def get_user_rank(self, user_id: int) -> int | None:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) + 1 FROM users WHERE balance > (SELECT balance FROM users WHERE user_id = ?)",
                (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return int(row[0]) if row else None

    # ==================== NEUES CHAT SYSTEM (max 180 Coins/Stunde) ====================

    async def get_chat_coins_last_hour(self, user_id: int) -> int:
        one_hour_ago = int(time.time()) - 3600
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM chat_earnings WHERE user_id = ? AND timestamp > ?",
                (user_id, one_hour_ago)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    async def can_earn_chat(self, user_id: int) -> bool:
        earned_last_hour = await self.get_chat_coins_last_hour(user_id)
        return earned_last_hour + CHAT_COINS <= CHAT_HOURLY_LIMIT

    async def claim_chat_earn(self, user_id: int) -> int:
        if not await self.can_earn_chat(user_id):
            return 0

        amount = CHAT_COINS
        await self.add_coins(user_id, amount)

        # Loggen für stündliches Limit
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO chat_earnings (user_id, amount, timestamp) VALUES (?, ?, ?)",
                (user_id, amount, int(time.time()))
            )
            await db.commit()

        return amount

    async def can_claim_daily(self, user_id: int) -> bool:
        last = await self.get_last_daily(user_id)
        if last is None:
            return True
        return (int(time.time()) - last) >= DAILY_COOLDOWN_SECONDS

    async def claim_daily(self, user_id: int) -> int:
        amount = random.randint(DAILY_COINS_MIN, DAILY_COINS_MAX)
        await self.add_coins(user_id, amount)
        await self.set_last_daily(user_id, int(time.time()))
        return amount

    # ==================== BACKGROUND TASKS ====================

    async def _voice_earnings_loop(self) -> None:
        await self.bot.wait_until_ready()
        print("[Economy] Voice earnings task started (2 Coins/Min bei ≥2 aktiven Personen).")

        while True:
            try:
                for guild in self.bot.guilds:
                    for vc in guild.voice_channels:
                        active_members = [
                            member for member in vc.members
                            if not member.bot
                            and member.voice
                            and not member.voice.self_deaf
                            and not member.voice.self_mute
                        ]

                        if len(active_members) >= 2:
                            for member in active_members:
                                await self.add_coins(member.id, VOICE_COINS_PER_MINUTE)

            except Exception as e:
                print(f"[Economy] Voice earnings error: {e}")

            await asyncio.sleep(60)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        await self.claim_chat_earn(message.author.id)

    # ==================== SLASH COMMANDS ====================

    @app_commands.command(name="set-currency", description="Ändere den Namen der Währung (Admin)")
    @app_commands.describe(name="Neuer Name der Währung")
    @app_commands.default_permissions(manage_guild=True)
    async def set_currency(self, interaction: discord.Interaction, name: str) -> None:
        if len(name) > 32:
            await interaction.response.send_message("❌ Name zu lang (max 32 Zeichen).", ephemeral=True)
            return

        await self.set_currency_name(name.strip())
        currency = await self.get_currency_name()

        embed = discord.Embed(title="✅ Währung geändert", description=f"Die Währung heißt jetzt **{currency}**.", color=discord.Color.green())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="economy-give", description="Gib einem User Coins (Admin)")
    @app_commands.describe(user="User", amount="Anzahl der Coins")
    @app_commands.default_permissions(manage_guild=True)
    async def economy_give(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, None]) -> None:
        currency = await self.get_currency_name()
        old_balance = await self.get_balance(user.id)
        new_balance = await self.add_coins(user.id, amount)

        embed = discord.Embed(title="✅ Coins gegeben", color=discord.Color.green())
        embed.add_field(name="User", value=user.mention, inline=True)
        embed.add_field(name="Betrag", value=f"+{amount:,} {currency}", inline=True)
        embed.add_field(name="Neuer Kontostand", value=f"**{new_balance:,}** {currency} (vorher: {old_balance:,})", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="economy-take", description="Nimm einem User Coins weg (Admin)")
    @app_commands.describe(user="User", amount="Anzahl der Coins")
    @app_commands.default_permissions(manage_guild=True)
    async def economy_take(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, None]) -> None:
        currency = await self.get_currency_name()
        old_balance = await self.get_balance(user.id)

        success = await self.remove_coins(user.id, amount)
        if not success:
            await interaction.response.send_message(f"❌ {user.mention} hat nicht genug {currency} (hat nur {old_balance:,}).", ephemeral=True)
            return

        new_balance = await self.get_balance(user.id)
        embed = discord.Embed(title="✅ Coins abgezogen", color=discord.Color.orange())
        embed.add_field(name="User", value=user.mention, inline=True)
        embed.add_field(name="Betrag", value=f"-{amount:,} {currency}", inline=True)
        embed.add_field(name="Neuer Kontostand", value=f"**{new_balance:,}** {currency} (vorher: {old_balance:,})", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="economy-set", description="Setze den exakten Kontostand eines Users (Admin)")
    @app_commands.describe(user="User", amount="Neuer exakter Kontostand")
    @app_commands.default_permissions(manage_guild=True)
    async def economy_set(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 0, None]) -> None:
        currency = await self.get_currency_name()
        old_balance = await self.get_balance(user.id)

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

    @app_commands.command(name="coinflip", description="Wähle Kopf oder Zahl und gewinne den 2x Einsatz")
    @app_commands.describe(bet="Einsatz (Min. 10)")
    @app_commands.checks.cooldown(1, 3.0, key=lambda interaction: interaction.user.id)
    async def coinflip(self, interaction: discord.Interaction, bet: app_commands.Range[int, 10, None]) -> None:
        user_id = interaction.user.id
        currency = await self.get_currency_name()

        current_balance = await self.get_balance(user_id)
        if current_balance < bet:
            await interaction.response.send_message(f"❌ Nicht genug {currency}! Dein Kontostand: **{current_balance:,}** {currency}.", ephemeral=True)
            return

        if not await self.remove_coins(user_id, bet):
            await interaction.response.send_message("❌ Fehler beim Abziehen des Einsatzes.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🪙 Coinflip - Wähle deine Seite",
            description=f"Du hast **{bet:,} {currency}** gesetzt.\n\nPasse deinen Einsatz an und wähle dann **Kopf** oder **Zahl**:",
            color=discord.Color.gold()
        )
        embed.set_footer(text="Timeout nach 2 Minuten")

        view = CoinflipView(self, interaction, bet, currency)
        await interaction.response.send_message(embed=embed, view=view)

    @coinflip.error
    async def coinflip_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[misc]
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(f"⏳ Warte noch **{error.retry_after:.1f}s**.", ephemeral=True)
        else:
            raise error

    @app_commands.command(name="leaderboard", description="Zeigt das interaktive Leaderboard an")
    @app_commands.checks.cooldown(1, 60.0, key=lambda interaction: interaction.user.id)
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        currency = await self.get_currency_name()
        total_users = await self.get_total_users()

        if total_users == 0:
            await interaction.response.send_message("Noch keine Daten im Leaderboard.", ephemeral=True)
            return

        view = LeaderboardView(self, interaction, currency)
        embed = await view.update_embed()

        await interaction.response.send_message(embed=embed, view=view)

    @leaderboard.error
    async def leaderboard_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[misc]
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(f"⏳ Das Leaderboard hat einen Cooldown von 60 Sekunden. Warte bitte noch **{error.retry_after:.0f}s**.", ephemeral=True)
        else:
            raise error

    @app_commands.command(name="balance", description="Zeigt deinen aktuellen Kontostand")
    @app_commands.describe(user="Optional: anderer User")
    async def balance(self, interaction: discord.Interaction, user: Optional[discord.Member] = None) -> None:
        target = user or interaction.user
        bal = await self.get_balance(target.id)
        currency = await self.get_currency_name()

        embed = discord.Embed(title=f"💰 {target.display_name}'s {currency}", description=f"**{bal:,}** {currency}", color=discord.Color.gold())
        embed.set_footer(text="/daily • /coinflip • /leaderboard")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="daily", description="Täglicher Bonus (einmal alle 24h)")
    async def daily(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False)

        user_id = interaction.user.id
        currency = await self.get_currency_name()

        if not await self.can_claim_daily(user_id):
            last = await self.get_last_daily(user_id)
            remaining = DAILY_COOLDOWN_SECONDS - (int(time.time()) - last) if last else 0
            hours = remaining // 3600
            minutes = (remaining % 3600) // 60
            await interaction.followup.send(f"⏳ Daily schon geholt. Nächster in **{hours}h {minutes}m**.", ephemeral=True)
            return

        amount = await self.claim_daily(user_id)
        new_balance = await self.get_balance(user_id)

        embed = discord.Embed(title="🎁 Täglicher Bonus", color=discord.Color.green())
        embed.description = f"**{interaction.user.mention}** hat **{amount} {currency}** erhalten!"
        embed.add_field(name="Neuer Kontostand", value=f"**{new_balance:,}** {currency}", inline=False)
        embed.set_footer(text="Bis morgen! 💰")
        await interaction.followup.send(embed=embed, ephemeral=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EconomyCog(bot), name="Economy")
