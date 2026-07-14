import discord
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cogs.economy import EconomyCog


class BetAdjustableMixin:
    """
    Mixin for Views that allow adjusting the bet amount.
    Provides +10, -10, x2 and ÷2 buttons.
    """

    def __init__(self, economy_cog: "EconomyCog", user_id: int, bet: int, currency: str) -> None:
        self.economy = economy_cog
        self.user_id = user_id
        self.bet = bet
        self.currency = currency

    async def _update_bet(self, interaction: discord.Interaction, new_bet: int) -> None:
        if new_bet < 10:
            new_bet = 10
        self.bet = new_bet
        await interaction.response.send_message(
            f"✅ Neuer Einsatz: **{self.bet:,}** {self.currency}",
            ephemeral=True
        )

    @discord.ui.button(label="➖ 10", style=discord.ButtonStyle.secondary, row=0)
    async def decrease_10(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Nur der Spieler darf die Buttons benutzen.",
                ephemeral=True
            )
            return
        await self._update_bet(interaction, self.bet - 10)

    @discord.ui.button(label="➕ 10", style=discord.ButtonStyle.secondary, row=0)
    async def increase_10(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Nur der Spieler darf die Buttons benutzen.",
                ephemeral=True
            )
            return
        await self._update_bet(interaction, self.bet + 10)

    @discord.ui.button(label="x2", style=discord.ButtonStyle.primary, row=0)
    async def double_bet(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Nur der Spieler darf die Buttons benutzen.",
                ephemeral=True
            )
            return
        await self._update_bet(interaction, self.bet * 2)

    @discord.ui.button(label="÷2", style=discord.ButtonStyle.primary, row=0)
    async def halve_bet(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Nur der Spieler darf die Buttons benutzen.",
                ephemeral=True
            )
            return
        new_bet = max(10, self.bet // 2)
        await self._update_bet(interaction, new_bet)
