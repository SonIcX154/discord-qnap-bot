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
LEADERBOARD_PER_PAGE = 10

# Earning rates
CHAT_COINS = 3
CHAT_COOLDOWN_SECONDS = 60

VOICE_COINS_PER_MINUTE = 3

DAILY_COINS_MIN = 80
DAILY_COINS_MAX = 150
DAILY_COOLDOWN_SECONDS = 24 * 60 * 60  # 24 hours


class LeaderboardView(discord.ui.View):
    """Interactive leaderboard with pagination and 'My Position' button."""

    def __init__(self, cog: "EconomyCog", interaction: discord.Interaction, currency: str):
        super().__init__(timeout=180)
        self.cog = cog
        self.interaction = interaction
        self.currency = currency
        self.current_page = 1
        self.total_pages = 1
        self.highlight_user_id = interaction.user.id

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

        lines = []
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
                prefix = "➤ **"
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
        return embed

    @discord.ui.button(label="◀️ Zurück", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 1:
            self.current_page -= 1
        embed = await self.update_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="▶️ Weiter", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        total = await self.get_total_pages()
        if self.current_page < total:
            self.current_page += 1
        embed = await self.update_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="📍 Meine Position", style=discord.ButtonStyle.primary)
    async def my_position_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        rank = await self.cog.get_user_rank(interaction.user.id)
        if rank is None:
            await interaction.response.send_message("Du hast noch keine Coins.", ephemeral=True)
            return

        new_page = (rank - 1) // LEADERBOARD_PER_PAGE + 1
        self.current_page = new_page
        self.highlight_user_id = interaction.user.id

        embed = await self.update_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class RouletteView(discord.ui.View):
    """Interactive Roulette betting view with buttons."""

    def __init__(self, cog: "EconomyCog", interaction: discord.Interaction, bet: int, currency: str):
        super().__init__(timeout=120)
        self.cog = cog
        self.interaction = interaction
        self.bet = bet
        self.currency = currency
        self.user_id = interaction.user.id
        self.bet_placed = False

    async def resolve_bet(self, interaction: discord.Interaction, bet_type: str, multiplier: int, description: str):
        if self.bet_placed:
            await interaction.response.send_message("Du hast bereits gesetzt!", ephemeral=True)
            return

        self.bet_placed = True
        for child in self.children:
            child.disabled = True

        number = random.randint(0, 36)
        color = "Grün" if number == 0 else ("Rot" if number % 2 == 1 else "Schwarz")

        won = False
        win_amount = 0

        if bet_type == "red" and color == "Rot":
            won = True
        elif bet_type == "black" and color == "Schwarz":
            won = True
        elif bet_type == "green" and number == 0:
            won = True
        elif bet_type == "even" and number != 0 and number % 2 == 0:
            won = True
        elif bet_type == "odd" and number % 2 == 1:
            won = True
        elif bet_type == "low" and 1 <= number <= 18:
            won = True
        elif bet_type == "high" and 19 <= number <= 36:
            won = True

        if won:
            win_amount = int(self.bet * multiplier)
            new_balance = await self.cog.add_coins(self.user_id, win_amount)
            color_embed = discord.Color.green()
            title = "🎉 Gewonnen!"
        else:
            new_balance = await self.cog.get_balance(self.user_id)
            color_embed = discord.Color.red()
            title = "😢 Verloren"

        embed = discord.Embed(title=title, color=color_embed)
        embed.description = f"**Die Kugel ist auf {number} ({color}) gelandet!**"
        embed.add_field(name="Dein Einsatz", value=f"{self.bet:,} {self.currency}", inline=True)
        if won:
            embed.add_field(name="Gewinn", value=f"+{win_amount:,} {self.currency} ({description})", inline=True)
        embed.add_field(name="Neuer Kontostand", value=f"**{new_balance:,}** {self.currency}", inline=False)
        embed.set_footer(text=f"Gespielt von {interaction.user.display_name}")

        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="🔴 Rot (2x)", style=discord.ButtonStyle.danger, row=0)
    async def red_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.resolve_bet(interaction, "red", 2, "Rot")

    @discord.ui.button(label="⚫ Schwarz (2x)", style=discord.ButtonStyle.secondary, row=0)
    async def black_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.resolve_bet(interaction, "black", 2, "Schwarz")

    @discord.ui.button(label="🟢 Grün/0 (36x)", style=discord.ButtonStyle.success, row=0)
    async def green_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.resolve_bet(interaction, "green", 36, "Grün (0)")

    @discord.ui.button(label="Gerade (2x)", style=discord.ButtonStyle.primary, row=1)
    async def even_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.resolve_bet(interaction, "even", 2, "Gerade")

    @discord.ui.button(label="Ungerade (2x)", style=discord.ButtonStyle.primary, row=1)
    async def odd_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.resolve_bet(interaction, "odd", 2, "Ungerade")

    @discord.ui.button(label="1-18 (2x)", style=discord.ButtonStyle.secondary, row=2)
    async def low_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.resolve_bet(interaction, "low", 2, "1-18")

    @discord.ui.button(label="19-36 (2x)", style=discord.ButtonStyle.secondary, row=2)
    async def high_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.resolve_bet(interaction, "high", 2, "19-36")

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class SlotsView(discord.ui.View):
    """View with bet adjustment + Play Again button for Slots."""

    def __init__(self, cog: "EconomyCog", user_id: int, bet: int, currency: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id
        self.bet = bet
        self.currency = currency
        self.playing = False

    async def _update_bet(self, interaction: discord.Interaction, new_bet: int):
        if new_bet < 10:
            new_bet = 10
        self.bet = new_bet

        await interaction.response.send_message(
            f"✅ Neuer Einsatz: **{self.bet:,}** {self.currency}",
            ephemeral=True
        )

    @discord.ui.button(label="➖ 10", style=discord.ButtonStyle.secondary, row=0)
    async def decrease_10(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Nur der Spieler darf die Buttons benutzen.", ephemeral=True)
            return
        await self._update_bet(interaction, self.bet - 10)

    @discord.ui.button(label="➕ 10", style=discord.ButtonStyle.secondary, row=0)
    async def increase_10(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Nur der Spieler darf die Buttons benutzen.", ephemeral=True)
            return
        await self._update_bet(interaction, self.bet + 10)

    @discord.ui.button(label="2x", style=discord.ButtonStyle.primary, row=0)
    async def double_bet(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Nur der Spieler darf die Buttons benutzen.", ephemeral=True)
            return
        await self._update_bet(interaction, self.bet * 2)

    @discord.ui.button(label="🔄 Nochmal spielen", style=discord.ButtonStyle.success, row=1)
    async def play_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Nur der Spieler darf den Button benutzen.", ephemeral=True)
            return

        if self.playing:
            await interaction.response.send_message("Bitte warte, bis die aktuelle Runde fertig ist.", ephemeral=True)
            return

        self.playing = True

        current_balance = await self.cog.get_balance(self.user_id)
        if current_balance < self.bet:
            await interaction.response.send_message(
                f"❌ Nicht genug {self.currency}! Du hast nur **{current_balance:,}**.",
                ephemeral=True
            )
            self.playing = False
            return

        if not await self.cog.remove_coins(self.user_id, self.bet):
            await interaction.response.send_message("❌ Fehler beim Abziehen des Einsatzes.", ephemeral=True)
            self.playing = False
            return

        spinning_embed = discord.Embed(
            title="🎰 Slots - Die Walzen drehen sich...",
            description="** | | | **",
            color=discord.Color.gold()
        )
        await interaction.response.edit_message(embed=spinning_embed, view=None)

        for _ in range(4):
            temp_reels = [random.choice(self.cog.SLOT_SYMBOLS) for _ in range(3)]
            spinning_embed.description = f"**{' | '.join(temp_reels)}**"
            await interaction.edit_original_response(embed=spinning_embed)
            await asyncio.sleep(0.28)

        reels, multiplier, win_text = self.cog._roll_slots()
        winnings = int(self.bet * multiplier)

        if multiplier > 0:
            new_balance = await self.cog.add_coins(self.user_id, winnings)
            color = discord.Color.green()
            title = "🎰 SLOTS - GEWONNEN!"
        else:
            new_balance = await self.cog.get_balance(self.user_id)
            color = discord.Color.red()
            title = "🎰 SLOTS - Verloren"

        final_embed = discord.Embed(title=title, color=color)
        final_embed.description = f"**{' | '.join(reels)}**"
        final_embed.add_field(name="Einsatz", value=f"{self.bet:,} {self.currency}", inline=True)
        if multiplier > 0:
            final_embed.add_field(name="Gewinn", value=f"+{winnings:,} {self.currency} ({win_text})", inline=True)
        else:
            final_embed.add_field(name="Ergebnis", value=win_text, inline=True)
        final_embed.add_field(name="Neuer Kontostand", value=f"**{new_balance:,}** {self.currency}", inline=False)
        final_embed.set_footer(text=f"Gespielt von {interaction.user.display_name} • RTP ~92%")

        new_view = SlotsView(self.cog, self.user_id, self.bet, self.currency)
        await interaction.edit_original_response(embed=final_embed, view=new_view)
        self.playing = False

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class CoinflipView(discord.ui.View):
    """Interactive Coinflip with choice of Kopf or Zahl."""

    def __init__(self, cog: "EconomyCog", interaction: discord.Interaction, bet: int, currency: str):
        super().__init__(timeout=120)
        self.cog = cog
        self.user_id = interaction.user.id
        self.bet = bet
        self.currency = currency

    async def _resolve(self, interaction: discord.Interaction, choice: str):
        for child in self.children:
            child.disabled = True

        result = random.choice(["Kopf", "Zahl"])
        won = (choice == result)

        if won:
            new_balance = await self.cog.add_coins(self.user_id, self.bet)
            color = discord.Color.green()
            title = "🪙 Coinflip - Gewonnen!"
            win_text = f"+{self.bet:,} {self.currency}"
        else:
            new_balance = await self.cog.get_balance(self.user_id)
            color = discord.Color.red()
            title = "🪙 Coinflip - Verloren"
            win_text = f"-{self.bet:,} {self.currency}"

        embed = discord.Embed(title=title, color=color)
        embed.description = f"Die Münze ist auf **{result}** gelandet."
        embed.add_field(name="Deine Wahl", value=choice, inline=True)
        embed.add_field(name="Ergebnis", value=win_text, inline=True)
        embed.add_field(name="Neuer Kontostand", value=f"**{new_balance:,}** {self.currency}", inline=False)
        embed.set_footer(text=f"Gespielt von {interaction.user.display_name}")

        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="🪙 Kopf", style=discord.ButtonStyle.primary, row=0)
    async def choose_kopf(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Nur der Spieler darf wählen.", ephemeral=True)
            return
        await self._resolve(interaction, "Kopf")

    @discord.ui.button(label="🪙 Zahl", style=discord.ButtonStyle.primary, row=0)
    async def choose_zahl(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Nur der Spieler darf wählen.", ephemeral=True)
            return
        await self._resolve(interaction, "Zahl")

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class EconomyCog(commands.Cog):
    """Economy & Gambling system with fictional (renamable) currency.
    
    Features:
    - Global user balances (one DB per bot instance)
    - Earn via chat + voice (only active users: not deaf/mute)
    - Interactive /leaderboard with pagination + My Position
    - Coinflip (choose Kopf/Zahl) + Slots (with bet buttons) + Roulette (with buttons)
    - Admin commands (/economy-give, /economy-take, /economy-set)
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
            "")
            await db.execute("""
                INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)
            """, ("currency_name", DEFAULT_CURRENCY))
            await db.commit()

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

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        user_id = message.author.id
        await self.claim_chat_earn(user_id)

    async def get_total_users(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM users WHERE balance > 0") as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    async def get_leaderboard_page(self, page: int, per_page: int = 10) -> List[Tuple[int, int]]:
        offset = (page - 1) * per_page
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT ? OFFSET ?",
                (per_page, offset)
            ) as cursor:
                rows = await cursor.fetchall()
                return [(row[0], row[1]) for row in rows]

    async def get_user_rank(self, user_id: int) -> Optional[int]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) + 1 FROM users WHERE balance > (SELECT balance FROM users WHERE user_id = ?)",
                (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    rank = row[0]
                    bal = await self.get_balance(user_id)
                    return rank if bal > 0 else None
                return None

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

    @app_commands.command(name="economy-give", description="Gib einem User Coins (Admin)")
    @app_commands.describe(user="User der Coins bekommen soll", amount="Anzahl der Coins")
    @app_commands.default_permissions(manage_guild=True)
    async def economy_give(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, None]):
        currency = await self.get_currency_name()
        old_balance = await self.get_balance(user.id)
        new_balance = await self.add_coins(user.id, amount)

        embed = discord.Embed(title="✅ Coins gegeben", color=discord.Color.green())
        embed.add_field(name="User", value=user.mention, inline=True)
        embed.add_field(name="Betrag", value=f"+{amount:,} {currency}", inline=True)
        embed.add_field(name="Neuer Kontostand", value=f"**{new_balance:,}** {currency} (vorher: {old_balance:,})", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="economy-take", description="Nimm einem User Coins weg (Admin)")
    @app_commands.describe(user="User von dem Coins abgezogen werden sollen", amount="Anzahl der Coins")
    @app_commands.default_permissions(manage_guild=True)
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

    @app_commands.command(name="economy-set", description="Setze den exakten Kontostand eines Users (Admin)")
    @app_commands.describe(user="User", amount="Neuer exakter Kontostand")
    @app_commands.default_permissions(manage_guild=True)
    async def economy_set(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 0, None]):
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

        embed = discord.Embed(
            title="🪙 Coinflip - Wähle deine Seite",
            description=f"Du hast **{bet:,} {currency}** gesetzt.

Wähle **Kopf** oder **Zahl**:",
            color=discord.Color.gold()
        )
        embed.set_footer(text="Timeout nach 2 Minuten")

        view = CoinflipView(self, interaction, bet, currency)
        await interaction.response.send_message(embed=embed, view=view)

    @coinflip.error
    async def coinflip_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"⏳ Warte noch **{error.retry_after:.1f}s**.", ephemeral=True)
        else:
            raise error

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

    @app_commands.command(name="slots", description="Spiele Slots mit animierten Walzen + Bet Buttons")
    @app_commands.describe(bet="Einsatz (Min. 10)")
    @app_commands.checks.cooldown(1, 4.0, key=lambda interaction: interaction.user.id)
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

        spinning_embed = discord.Embed(
            title="🎰 Slots - Die Walzen drehen sich...",
            description="** | | | **",
            color=discord.Color.gold()
        )
        await interaction.response.send_message(embed=spinning_embed)

        for _ in range(4):
            temp_reels = [random.choice(self.SLOT_SYMBOLS) for _ in range(3)]
            spinning_embed.description = f"**{' | '.join(temp_reels)}**"
            await interaction.edit_original_response(embed=spinning_embed)
            await asyncio.sleep(0.28)

        reels, multiplier, win_text = self._roll_slots()
        winnings = int(bet * multiplier)

        if multiplier > 0:
            new_balance = await self.add_coins(user_id, winnings)
            color = discord.Color.green()
            title = "🎰 SLOTS - GEWONNEN!"
        else:
            new_balance = await self.get_balance(user_id)
            color = discord.Color.red()
            title = "🎰 SLOTS - Verloren"

        final_embed = discord.Embed(title=title, color=color)
        final_embed.description = f"**{' | '.join(reels)}**"
        final_embed.add_field(name="Einsatz", value=f"{bet:,} {currency}", inline=True)
        if multiplier > 0:
            final_embed.add_field(name="Gewinn", value=f"+{winnings:,} {self.currency} ({win_text})", inline=True)
        else:
            final_embed.add_field(name="Ergebnis", value=win_text, inline=True)
        final_embed.add_field(name="Neuer Kontostand", value=f"**{new_balance:,}** {currency}", inline=False)
        final_embed.set_footer(text=f"Gespielt von {interaction.user.display_name} • RTP ~92%")

        view = SlotsView(self, user_id, bet, currency)
        await interaction.edit_original_response(embed=final_embed, view=view)

    @slots.error
    async def slots_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(f"⏳ Warte **{error.retry_after:.1f}s**.", ephemeral=True)
        else:
            raise error

    @app_commands.command(name="roulette", description="Spiele Roulette mit interaktiven Buttons")
    @app_commands.describe(bet="Dein Einsatz (Min. 10)")
    @app_commands.checks.cooldown(1, 5.0, key=lambda interaction: interaction.user.id)
    async def roulette(self, interaction: discord.Interaction, bet: app_commands.Range[int, 10, None]):
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
            await interaction.response.send_message("❌ Fehler beim Abziehen des Einsatzes.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🎰 Roulette - Wähle deinen Einsatz",
            description="Du hast **{} {}** gesetzt.\n\nWähle jetzt, worauf du setzen möchtest:".format(bet, currency),
            color=discord.Color.gold()
        )
        embed.set_footer(text="Die Kugel rollt... • Timeout nach 2 Minuten")

        view = RouletteView(self, interaction, bet, currency)
        await interaction.response.send_message(embed=embed, view=view)

    @roulette.error
    async def roulette_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"⏳ Warte noch **{error.retry_after:.1f}s** bevor du wieder roulette spielst.",
                ephemeral=True
            )
        else:
            raise error

    @app_commands.command(name="leaderboard", description="Zeigt das interaktive Leaderboard an")
    @app_commands.checks.cooldown(1, 60.0, key=lambda interaction: interaction.user.id)
    async def leaderboard(self, interaction: discord.Interaction):
        currency = await self.get_currency_name()
        total_users = await self.get_total_users()

        if total_users == 0:
            await interaction.response.send_message("Noch keine Daten im Leaderboard.", ephemeral=True)
            return

        view = LeaderboardView(self, interaction, currency)
        embed = await view.update_embed()

        await interaction.response.send_message(embed=embed, view=view)

    @leaderboard.error
    async def leaderboard_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"⏳ Das Leaderboard hat einen Cooldown von 60 Sekunden. Warte bitte noch **{error.retry_after:.0f}s**.",
                ephemeral=True
            )
        else:
            raise error

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
        await interaction.response.defer(ephemeral=False)

        user_id = interaction.user.id
        currency = await self.get_currency_name()

        if not await self.can_claim_daily(user_id):
            last = await self.get_last_daily(user_id)
            remaining = DAILY_COOLDOWN_SECONDS - (int(time.time()) - last) if last else 0
            hours = remaining // 3600
            minutes = (remaining % 3600) // 60
            await interaction.followup.send(
                f"⏳ Daily schon geholt. Nächster in **{hours}h {minutes}m**.",
                ephemeral=True
            )
            return

        amount = await self.claim_daily(user_id)
        new_balance = await self.get_balance(user_id)

        embed = discord.Embed(title="🎁 Täglicher Bonus", color=discord.Color.green())
        embed.description = f"**{interaction.user.mention}** hat **{amount} {currency}** erhalten!"
        embed.add_field(name="Neuer Kontostand", value=f"**{new_balance:,}** {currency}", inline=False)
        embed.set_footer(text="Bis morgen! 💰")
        await interaction.followup.send(embed=embed, ephemeral=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCog(bot))
