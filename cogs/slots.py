import random
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from typing import List, Tuple


class SlotsView(discord.ui.View):
    """Interactive Slots view with bet adjustment and Play Again button."""

    def __init__(self, economy_cog, user_id: int, bet: int, currency: str):
        super().__init__(timeout=300)
        self.economy = economy_cog
        self.user_id = user_id
        self.bet = bet
        self.currency = currency
        self.playing = False

    async def _update_bet(self, interaction: discord.Interaction, new_bet: int):
        if new_bet < 10:
            new_bet = 10
        self.bet = new_bet

        balance = await self.economy.get_balance(self.user_id)
        embed = discord.Embed(
            title="🎰 Slots",
            description=f"Aktueller Einsatz: **{self.bet:,} {self.currency}**\nKontostand: **{balance:,} {self.currency}**",
            color=discord.Color.gold()
        )
        embed.set_footer(text="Ändere den Einsatz oder starte eine neue Runde")
        await interaction.response.edit_message(embed=embed, view=self)

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

    @discord.ui.button(label="÷2", style=discord.ButtonStyle.secondary, row=0)
    async def halve_bet(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Nur der Spieler darf die Buttons benutzen.", ephemeral=True)
            return
        await self._update_bet(interaction, self.bet // 2)

    @discord.ui.button(label="🔄 Nochmal spielen", style=discord.ButtonStyle.success, row=1)
    async def play_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Nur der Spieler darf den Button benutzen.", ephemeral=True)
            return

        if self.playing:
            await interaction.response.send_message("Bitte warte, bis die aktuelle Runde fertig ist.", ephemeral=True)
            return

        self.playing = True
        try:
            current_balance = await self.economy.get_balance(self.user_id)
            if current_balance < self.bet:
                await interaction.response.send_message(f"❌ Nicht genug {self.currency}! Du hast nur **{current_balance:,}**.", ephemeral=True)
                return

            if not await self.economy.remove_coins(self.user_id, self.bet):
                await interaction.response.send_message("❌ Fehler beim Abziehen des Einsatzes.", ephemeral=True)
                return

            spinning_embed = discord.Embed(title="🎰 Slots - Die Walzen drehen sich...", description="** | | | **", color=discord.Color.gold())
            await interaction.response.edit_message(embed=spinning_embed, view=None)

            for _ in range(4):
                temp_reels = [random.choice(SlotsCog.SLOT_SYMBOLS) for _ in range(3)]
                spinning_embed.description = f"**{' | '.join(temp_reels)}**"
                await interaction.edit_original_response(embed=spinning_embed)
                await asyncio.sleep(0.28)

            reels, multiplier, win_text = self._roll_slots()
            winnings = int(self.bet * multiplier)

            if multiplier > 0:
                new_balance = await self.economy.add_coins(self.user_id, winnings)
                color = discord.Color.green()
                title = "🎰 SLOTS - GEWONNEN!"
            else:
                new_balance = await self.economy.get_balance(self.user_id)
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

            new_view = SlotsView(self.economy, self.user_id, self.bet, self.currency)
            await interaction.edit_original_response(embed=final_embed, view=new_view)
        finally:
            self.playing = False

    def _roll_slots(self) -> Tuple[List[str], int, str]:
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

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class SlotsCog(commands.Cog):
    """Slots minigame cog."""

    SLOT_SYMBOLS: List[str] = ["🍒", "🍋", "🔔", "⭐", "7️⃣", "💎"]

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        print("[Slots] Slots Cog loaded.")

    @app_commands.command(name="slots", description="Spiele Slots mit animierten Walzen + Bet Buttons")
    @app_commands.describe(bet="Einsatz (Min. 10)")
    @app_commands.checks.cooldown(1, 4.0, key=lambda interaction: interaction.user.id)
    async def slots(self, interaction: discord.Interaction, bet: app_commands.Range[int, 10, None]):
        economy = getattr(self.bot, "_economy_cog", None)
        if not economy:
            economy = self.bot.get_cog("EconomyCog")
        if not economy:
            await interaction.response.send_message("Economy system nicht verfügbar.", ephemeral=True)
            return

        user_id = interaction.user.id
        currency = await economy.get_currency_name()

        current = await economy.get_balance(user_id)
        if current < bet:
            await interaction.response.send_message(f"❌ Nicht genug {currency}! Du hast **{current:,}** {currency}.", ephemeral=True)
            return

        if not await economy.remove_coins(user_id, bet):
            await interaction.response.send_message("❌ Fehler beim Einsatz.", ephemeral=True)
            return

        spinning_embed = discord.Embed(title="🎰 Slots - Die Walzen drehen sich...", description="** | | | **", color=discord.Color.gold())
        await interaction.response.send_message(embed=spinning_embed)

        for _ in range(4):
            temp_reels = [random.choice(self.SLOT_SYMBOLS) for _ in range(3)]
            spinning_embed.description = f"**{' | '.join(temp_reels)}**"
            await interaction.edit_original_response(embed=spinning_embed)
            await asyncio.sleep(0.28)

        reels, multiplier, win_text = self._roll_slots()
        winnings = int(bet * multiplier)

        if multiplier > 0:
            new_balance = await economy.add_coins(user_id, winnings)
            color = discord.Color.green()
            title = "🎰 SLOTS - GEWONNEN!"
        else:
            new_balance = await economy.get_balance(user_id)
            color = discord.Color.red()
            title = "🎰 SLOTS - Verloren"

        final_embed = discord.Embed(title=title, color=color)
        final_embed.description = f"**{' | '.join(reels)}**"
        final_embed.add_field(name="Einsatz", value=f"{bet:,} {currency}", inline=True)
        if multiplier > 0:
            final_embed.add_field(name="Gewinn", value=f"+{winnings:,} {currency} ({win_text})", inline=True)
        else:
            final_embed.add_field(name="Ergebnis", value=win_text, inline=True)
        final_embed.add_field(name="Neuer Kontostand", value=f"**{new_balance:,}** {currency}", inline=False)
        final_embed.set_footer(text=f"Gespielt von {interaction.user.display_name} • RTP ~92%")

        view = SlotsView(economy, user_id, bet, currency)
        await interaction.edit_original_response(embed=final_embed, view=view)

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

    @slots.error
    async def slots_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(f"⏳ Warte **{error.retry_after:.1f}s**.", ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(SlotsCog(bot))
