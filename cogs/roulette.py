from __future__ import annotations
import os
import random
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

class RouletteView(BetAdjustableMixin, discord.ui.View):
    """Interactive Roulette with bet adjustment."""

    def __init__(self, economy_cog: "EconomyCog", interaction: discord.Interaction, bet: int, currency: str) -> None:
        BetAdjustableMixin.__init__(self, economy_cog, interaction.user.id, bet, currency)
        discord.ui.View.__init__(self, timeout=180)
        self.interaction = interaction

    async def _get_updated_embed(self) -> discord.Embed:
        """Live update embed when adjusting bet - stable layout."""
        current_balance = await self.economy.get_balance(self.user_id)
        
        embed = discord.Embed(
            title=ROULETTE_EMOTE + " Roulette",
            color=discord.Color.gold()
        )
        embed.add_field(name="Einsatz", value=f"{self.bet:,} {self.currency}", inline=True)
        embed.add_field(
            name="Kontostand", 
            value=f"**{current_balance:,}** {self.currency}", 
            inline=False
        )
        embed.set_footer(text="Glücksspiel kann süchtig machen")
        return embed

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
            new_balance = await self.economy.add_coins(self.user_id, win_amount)
            color_embed = discord.Color.green()
            title = ROULETTE_EMOTE + " Roulette - 🎉 Gewonnen!"
        else:
            new_balance = await self.economy.get_balance(self.user_id)
            color_embed = discord.Color.red()
            title = ROULETTE_EMOTE + " Roulette - 😢 Verloren"

        embed = discord.Embed(title=title, color=color_embed)
        embed.description = f"**Die Kugel ist auf {number} ({color}) gelandet!**"
        embed.add_field(name="Einsatz", value=f"{self.bet:,} {self.currency}", inline=True)
        if won:
            embed.add_field(name="Gewinn", value=f"+{win_amount:,} {self.currency} ({description})", inline=True)
        embed.add_field(name="Kontostand", value=f"**{new_balance:,}** {self.currency}", inline=False)
        embed.set_footer(text=f"Gespielt von {interaction.user.display_name}")

        new_view = RouletteView(self.economy, interaction, self.bet, self.currency)
        await interaction.response.edit_message(embed=embed, view=new_view)

    @discord.ui.button(label="🔴 Rot (2x)", style=discord.ButtonStyle.danger, row=1)
    async def red_button(self, interaction: discord.Interaction, button: discord.ui.Button[Any]) -> None:
        await self.resolve_bet(interaction, "red", 2, "Rot")

    @discord.ui.button(label="⚫ Schwarz (2x)", style=discord.ButtonStyle.secondary, row=1)
    async def black_button(self, interaction: discord.Interaction, button: discord.ui.Button[Any]) -> None:
        await self.resolve_bet(interaction, "black", 2, "Schwarz")

    @discord.ui.button(label="🟢 Grün/0 (36x)", style=discord.ButtonStyle.success, row=1)
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

        embed = discord.Embed(
            title=ROULETTE_EMOTE + " Roulette",
            description=f"Du hast **{bet:,} {currency}** als Einsatz gewählt.\n\nPasse deinen Einsatz an und wähle dann, worauf du setzen möchtest:",
            color=discord.Color.gold()
        )
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