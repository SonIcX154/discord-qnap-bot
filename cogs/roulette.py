from __future__ import annotations
import os
import random
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cogs.economy import EconomyCog

try:
    from utils.bet_mixin import BetAdjustableMixin
except ImportError:
    from ..utils.bet_mixin import BetAdjustableMixin


ROULETTE_EMOTE = str(os.getenv("ROULETTE_EMOTE", "🎰"))

# Standard Roulette-Rad-Reihenfolge (realistisch)
ROULETTE_WHEEL = [
    0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23,
    10, 5, 24, 16, 33, 1, 20, 14, 31, 9, 22, 18, 29, 7, 28, 12, 35, 3, 26
]

# Offizielle Rot/Schwarz-Zuordnung (nicht nach Parität, sondern nach echter Casino-Norm)
RED_NUMBERS = frozenset({1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36})


def _color_square(number: int) -> str:
    """Gibt das Farb-Emoji passend zur offiziellen Rot/Schwarz-Zuordnung zurück."""
    if number == 0:
        return "🟩"
    return "🟥" if number in RED_NUMBERS else "⬛"


class RouletteView(BetAdjustableMixin, discord.ui.View):
    """Interactive Roulette with bet adjustment."""

    def __init__(self, economy_cog: "EconomyCog", interaction: discord.Interaction, bet: int, currency: str) -> None:
        BetAdjustableMixin.__init__(self, economy_cog, interaction.user.id, bet, currency)
        discord.ui.View.__init__(self, timeout=180)
        self.interaction = interaction

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only the user who started /roulette may press buttons."""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Um zu spielen führe den Command bitte selber aus.",
                ephemeral=True,
            )
            return False
        return True

    async def _get_updated_embed(self) -> discord.Embed:
        """Live update embed when adjusting bet - stable layout."""
        current_balance = await self.economy.get_balance(self.user_id)
        
        embed = discord.Embed(
            title=ROULETTE_EMOTE + " Roulette",
            color=discord.Color.gold()
        )
        embed.add_field(name="Einsatz", value=f"{self.bet:,} {self.currency}", inline=True)
        embed.add_field(name="Gewinn", value="...", inline=True)
        embed.add_field(
            name="Kontostand", 
            value=f"**{current_balance:,}** {self.currency}",
            inline=False
        )
        embed.set_footer(text="Glücksspiel kann süchtig machen")
        return embed

    async def _spin_animation(self, interaction: discord.Interaction, winning_number: int, balance_after: int, description: str) -> None:
        """Roulette-Animation: Die Kugel bleibt mittig und wackelt nur leicht hin und her,
        während das Rad darunter "durchläuft". Landet garantiert im letzten Frame auf der Gewinnzahl."""
        wheel_len = len(ROULETTE_WHEEL)
        try:
            win_index = ROULETTE_WHEEL.index(winning_number)
        except ValueError:
            win_index = 0

        WINDOW = 7
        CENTER = WINDOW // 2  # 4
        FRAMES = 16

        total_spin = 30
        ease_power = 1.1  # kleiner = langsamerer Start, 2.0 = alter (schneller) Start, 1.0 = konstante Geschwindigkeit (keine Verlangsamung mehr)
        offsets = []
        for i in range(FRAMES):
            t = i / (FRAMES - 1)
            eased = total_spin * (1 - t) ** ease_power
            offsets.append(round(eased))
        offsets[-1] = 0  # letzter Frame: exakt ausgerichtet
        offsets[-2] = 0  # vorletzter Frame: exakt ausgerichtet, um die Kugel stabil zu halten

        # Kugel-Wackeln: minimale Bewegung um die Mitte, in den letzten Frames stabilisiert sie sich.
        jitter_pattern = [0, 1, 0, -1, 0, -1, 0, 1, 0, -1, 0, -1, 0, 0, 0, 0][:FRAMES]
        jitter_pattern[-1] = 0
        jitter_pattern[-2] = 0

        for step in range(FRAMES):
            offset = offsets[step]
            window_numbers = [
                ROULETTE_WHEEL[(win_index - CENTER + offset + i) % wheel_len]
                for i in range(WINDOW)
            ]

            ball_index = max(0, min(WINDOW - 1, CENTER + jitter_pattern[step]))
            is_final_frame = step == FRAMES - 1

            cells = []
            for i, num in enumerate(window_numbers):
                square = _color_square(num)
                if i == ball_index:
                    cells.append(f"⚪**{num}**{square}" if is_final_frame else f"⚪{num}{square}")
                else:
                    cells.append(f"{num}{square}")

            wheel_str = " | ".join(cells)

            embed = discord.Embed(
                title=f"{ROULETTE_EMOTE} Roulette - Die Kugel rollt...",
                description=wheel_str,
                color=discord.Color.gold()
            )
            embed.add_field(name="Einsatz", value=f"{self.bet:,} {self.currency}", inline=True)
            embed.add_field(name="Gewinn", value=f"bei {description}", inline=True)
            embed.add_field(name="Kontostand", value=f"**{balance_after:,}** {self.currency}", inline=False)
            embed.set_footer(text="Glücksspiel kann süchtig machen")

            if step == 0:
                await interaction.response.edit_message(embed=embed, view=None)
            else:
                await interaction.edit_original_response(embed=embed)

            base_delay = 0.16
            if step >= FRAMES - 4:
                delay = base_delay * 3
            elif step >= FRAMES // 2:
                delay = base_delay * 2
            else:
                delay = base_delay
            await asyncio.sleep(delay) # Langsamer werden

        # Kurze Pause auf der finalen Zahl
        await asyncio.sleep(0.6)

    async def resolve_bet(self, interaction: discord.Interaction, bet_type: str, multiplier: int, description: str) -> None:
        current_balance = await self.economy.get_balance(self.user_id)
        if current_balance < self.bet:
            await interaction.response.send_message(
                f"❌ Nicht genug {self.currency}! Du hast nur **{current_balance:,}**.",
                ephemeral=True
            )
            return

        if not await self.economy.remove_coins(self.user_id, self.bet):
            await interaction.response.send_message("❌ Fehler beim Abziehen des Einsatzes.", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]

        # Gewinnzahl bestimmen
        number = random.randint(0, 36)
        balance_after_deduction = current_balance - self.bet

        # Animation abspielen (landet garantiert auf der richtigen Zahl)
        await self._spin_animation(interaction, number, balance_after_deduction, description)

        color = "Grün" if number == 0 else ("Rot" if number in RED_NUMBERS else "Schwarz")

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
            new_balance = await self.economy.add_coins(self.user_id, win_amount)
            color_embed = discord.Color.green()
            title = ROULETTE_EMOTE + " Roulette - 🎉 Gewonnen!"
            gewinn_text = f"+{win_amount:,} {self.currency} ({description})"
        else:
            new_balance = await self.economy.get_balance(self.user_id)
            color_embed = discord.Color.red()
            title = ROULETTE_EMOTE + " Roulette - 😢 Verloren"
            gewinn_text = f"0 {self.currency}"

        square = _color_square(number)
        embed = discord.Embed(title=title, color=color_embed)
        embed.description = f"**Die Kugel ist auf {number} {square} gelandet!**"
        embed.add_field(name="Einsatz", value=f"{self.bet:,} {self.currency}", inline=True)
        embed.add_field(name="Gewinn", value=gewinn_text, inline=True)
        embed.add_field(name="Kontostand", value=f"**{new_balance:,}** {self.currency}", inline=False)
        embed.set_footer(text=f"Gespielt von {interaction.user.display_name}")

        new_view = RouletteView(self.economy, interaction, self.bet, self.currency)
        await interaction.edit_original_response(embed=embed, view=new_view)

    @discord.ui.button(label="🟥 Rot (2x)", style=discord.ButtonStyle.danger, row=1)
    async def red_button(self, interaction: discord.Interaction, button: discord.ui.Button[Any]) -> None:
        await self.resolve_bet(interaction, "red", 2, "Rot")

    @discord.ui.button(label="⬛ Schwarz (2x)", style=discord.ButtonStyle.secondary, row=1)
    async def black_button(self, interaction: discord.Interaction, button: discord.ui.Button[Any]) -> None:
        await self.resolve_bet(interaction, "black", 2, "Schwarz")

    @discord.ui.button(label="🟩 Grün/0 (36x)", style=discord.ButtonStyle.success, row=1)
    async def green_button(self, interaction: discord.Interaction, button: discord.ui.Button[Any]) -> None:
        await self.resolve_bet(interaction, "green", 36, "Grün (0)")

    @discord.ui.button(label="Gerade (2x)", style=discord.ButtonStyle.primary, row=2)
    async def even_button(self, interaction: discord.Interaction, button: discord.ui.Button[Any]) -> None:
        await self.resolve_bet(interaction, "even", 2, "Gerade")

    @discord.ui.button(label="Ungerade (2x)", style=discord.ButtonStyle.primary, row=2)
    async def odd_button(self, interaction: discord.Interaction, button: discord.ui.Button[Any]) -> None:
        await self.resolve_bet(interaction, "odd", 2, "Ungerade")

    @discord.ui.button(label="1-18 (2x)", style=discord.ButtonStyle.secondary, row=2)
    async def low_button(self, interaction: discord.Interaction, button: discord.ui.Button[Any]) -> None:
        await self.resolve_bet(interaction, "low", 2, "1-18")

    @discord.ui.button(label="19-36 (2x)", style=discord.ButtonStyle.secondary, row=2)
    async def high_button(self, interaction: discord.Interaction, button: discord.ui.Button[Any]) -> None:
        await self.resolve_bet(interaction, "high", 2, "19-36")

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]


class RouletteCog(commands.Cog):
    """Roulette minigame cog."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        print("[Roulette] Roulette Cog loaded.")

    @app_commands.command(name="roulette", description="Spiele Roulette mit interaktiven Buttons + Bet Anpassung")
    @app_commands.describe(bet="Dein Einsatz (Min. 10)")
    @app_commands.checks.cooldown(1, 5.0, key=lambda interaction: interaction.user.id)
    async def roulette(self, interaction: discord.Interaction, bet: app_commands.Range[int, 10, None]) -> None:
        economy = self.bot.get_cog("EconomyCog")
        if not economy:
            await interaction.response.send_message("Economy system nicht verfügbar.", ephemeral=True)
            return

        user_id = interaction.user.id
        currency = await economy.get_currency_name()  # type: ignore[union-attr]

        current = await economy.get_balance(user_id)  # type: ignore[union-attr]
        if current < bet:
            await interaction.response.send_message(f"❌ Nicht genug {currency}! Du hast **{current:,}** {currency}.", ephemeral=True)
            return

        embed = discord.Embed(title=ROULETTE_EMOTE + " Roulette", color=discord.Color.gold())
        embed.add_field(name="Einsatz", value=f"**{bet:,}** {currency}", inline=True)
        embed.add_field(name="Gewinn", value="...", inline=True)
        embed.add_field(name="Kontostand", value=f"**{current:,}** {currency}", inline=False)
        embed.set_footer(text="Glücksspiel kann süchtig machen")

        view = RouletteView(economy, interaction, bet, currency)  # type: ignore[arg-type]
        await interaction.response.send_message(embed=embed, view=view)

    @roulette.error
    async def roulette_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[misc]
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(f"⏳ Warte noch **{error.retry_after:.1f}s** bevor du wieder roulette spielst.", ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RouletteCog(bot))
