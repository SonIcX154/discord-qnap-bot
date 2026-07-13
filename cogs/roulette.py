import random
import discord
from discord.ext import commands
from discord import app_commands


# TODO: Roulette Animation verbessern
# Aktuell wird das Ergebnis direkt angezeigt.
# Besser wäre eine richtige Animation (z.B. rollende Kugel / Zahlen die nacheinander angezeigt werden,
# ähnlich wie bei echten Roulette-Tischen oder mit mehreren Schritten + Verzögerung).
# Das würde das Spiel deutlich cooler und spannender machen.


class RouletteView(discord.ui.View):
    """Interactive Roulette betting view with buttons."""

    def __init__(self, economy_cog, interaction: discord.Interaction, bet: int, currency: str):
        super().__init__(timeout=120)
        self.economy = economy_cog
        self.user_id = interaction.user.id
        self.bet = bet
        self.currency = currency
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
            new_balance = await self.economy.add_coins(self.user_id, win_amount)
            color_embed = discord.Color.green()
            title = "🎉 Gewonnen!"
        else:
            new_balance = await self.economy.get_balance(self.user_id)
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


class RouletteCog(commands.Cog):
    """Roulette minigame cog."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        print("[Roulette] Roulette Cog loaded.")

    @app_commands.command(name="roulette", description="Spiele Roulette mit interaktiven Buttons")
    @app_commands.describe(bet="Dein Einsatz (Min. 10)")
    @app_commands.checks.cooldown(1, 5.0, key=lambda interaction: interaction.user.id)
    async def roulette(self, interaction: discord.Interaction, bet: app_commands.Range[int, 10, None]):
        economy = self.bot.get_cog("Economy")
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
            await interaction.response.send_message("❌ Fehler beim Abziehen des Einsatzes.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🎰 Roulette - Wähle deinen Einsatz",
            description=f"Du hast **{bet:,} {currency}** gesetzt.\n\nWähle jetzt, worauf du setzen möchtest:",
            color=discord.Color.gold()
        )
        embed.set_footer(text="Die Kugel rollt... • Timeout nach 2 Minuten")

        view = RouletteView(economy, interaction, bet, currency)
        await interaction.response.send_message(embed=embed, view=view)

    @roulette.error
    async def roulette_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(f"⏳ Warte noch **{error.retry_after:.1f}s** bevor du wieder roulette spielst.", ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(RouletteCog(bot))
