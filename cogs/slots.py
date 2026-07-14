from __future__ import annotations

import random
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cogs.economy import EconomyCog

try:
    from utils.bet_mixin import BetAdjustableMixin
    from utils.replay_mixin import ReplayMixin
except ImportError:
    from ..utils.bet_mixin import BetAdjustableMixin
    from ..utils.replay_mixin import ReplayMixin


class SlotsView(BetAdjustableMixin, ReplayMixin, discord.ui.View):
    """Interactive Slots view with bet adjustment and replay."""

    def __init__(self, economy_cog: "EconomyCog", user_id: int, bet: int, currency: str) -> None:
        BetAdjustableMixin.__init__(self, economy_cog, user_id, bet, currency)
        ReplayMixin.__init__(self, user_id)
        discord.ui.View.__init__(self, timeout=300)

    async def _do_replay(self, interaction: discord.Interaction) -> None:
        current_balance = await self.economy.get_balance(self.user_id)
        if current_balance < self.bet:
            await interaction.response.send_message(f"❌ Nicht genug {self.currency}! Du hast nur **{current_balance:,}**.", ephemeral=True)
            return

        if not await self.economy.remove_coins(self.user_id, self.bet):
            await interaction.response.send_message("❌ Fehler beim Abziehen des Einsatzes.", ephemeral=True)
            return

        spinning_embed = discord.Embed(title="🔀 Slots - Die Walzen drehen sich...", description="** | | | **", color=discord.Color.gold())
        await interaction.response.edit_message(embed=spinning_embed, view=None)

        for _ in range(4):
            temp_reels = [random.choice(SlotsCog.SLOT_SYMBOLS) for _ in range(3)]
            spinning_embed.description = f"**{' | '.join(temp_reels)}**"
            await interaction.edit_original_response(embed=spinning_embed)
            await asyncio.sleep(0.28)

        reels, multiplier, win_text = SlotsCog.roll_slots()
        winnings = int(self.bet * multiplier)

        if multiplier > 0:
            new_balance = await self.economy.add_coins(self.user_id, winnings)
            color = discord.Color.green()
            title = "🔀 SLOTS - GEWONNEN!"
        else:
            new_balance = await self.economy.get_balance(self.user_id)
            color = discord.Color.red()
            title = "🔀 SLOTS - Verloren"

        final_embed = discord.Embed(title=title, color=color)
        final_embed.description = f"**{' | '.join(reels)}**"
        final_embed.add_field(name="Einsatz", value=f"{self.bet:,} {self.currency}", inline=True)
        if multiplier > 0:
            final_embed.add_field(name="Gewinn", value=f"+{winnings:,} {self.currency} ({win_text})", inline=True)
        else:
            final_embed.add_field(name="Ergebnis", value=win_text, inline=True)
        final_embed.add_field(name="Neuer Kontostand", value=f"**{new_balance:,}** {self.currency}", inline=False)
        final_embed.set_footer(text=f"Gespielt von {interaction.user.display_name} • RTP ~92%")

        new_view = SlotsView(self.economy, self.user_id, self.bet, self.currency)
        await interaction.edit_original_response(embed=final_embed, view=new_view)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]


class SlotsCog(commands.Cog):
    """Slots minigame cog."""

    SLOT_SYMBOLS: list[str] = ["🍒", "🍋", "🔔", "⭐", "7️⃣", "💎"]

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        print("[Slots] Slots Cog loaded.")

    @app_commands.command(name="slots", description="Spiele Slots mit animierten Walzen + Bet Buttons")
    @app_commands.describe(bet="Einsatz (Min. 10)")
    @app_commands.checks.cooldown(1, 4.0, key=lambda interaction: interaction.user.id)
    async def slots(self, interaction: discord.Interaction, bet: app_commands.Range[int, 10, None]) -> None:
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

        if not await economy.remove_coins(user_id, bet):  # type: ignore[union-attr]
            await interaction.response.send_message("❌ Fehler beim Einsatz.", ephemeral=True)
            return

        spinning_embed = discord.Embed(title="🔀 Slots - Die Walzen drehen sich...", description="** | | | **", color=discord.Color.gold())
        await interaction.response.send_message(embed=spinning_embed)

        for _ in range(4):
            temp_reels = [random.choice(self.SLOT_SYMBOLS) for _ in range(3)]
            spinning_embed.description = f"**{' | '.join(temp_reels)}**"
            await interaction.edit_original_response(embed=spinning_embed)
            await asyncio.sleep(0.28)

        reels, multiplier, win_text = self.roll_slots()
        winnings = int(bet * multiplier)

        if multiplier > 0:
            new_balance = await economy.add_coins(user_id, winnings)  # type: ignore[union-attr]
            color = discord.Color.green()
            title = "🔀 SLOTS - GEWONNEN!"
        else:
            new_balance = await economy.get_balance(user_id)  # type: ignore[union-attr]
            color = discord.Color.red()
            title = "🔀 SLOTS - Verloren"

        final_embed = discord.Embed(title=title, color=color)
        final_embed.description = f"**{' | '.join(reels)}**"
        final_embed.add_field(name="Einsatz", value=f"{bet:,} {currency}", inline=True)
        if multiplier > 0:
            final_embed.add_field(name="Gewinn", value=f"+{winnings:,} {currency} ({win_text})", inline=True)
        else:
            final_embed.add_field(name="Ergebnis", value=win_text, inline=True)
        final_embed.add_field(name="Neuer Kontostand", value=f"**{new_balance:,}** {currency}", inline=False)
        final_embed.set_footer(text=f"Gespielt von {interaction.user.display_name} • RTP ~92%")

        view = SlotsView(economy, user_id, bet, currency)  # type: ignore[arg-type]
        await interaction.edit_original_response(embed=final_embed, view=view)

    @staticmethod
    def roll_slots() -> tuple[list[str], int, str]:
        reels = [random.choice(SlotsCog.SLOT_SYMBOLS) for _ in range(3)]

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

    @slots.error
    async def slots_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[misc]
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(f"⏳ Warte **{error.retry_after:.1f}s**.", ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SlotsCog(bot))
