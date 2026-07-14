import discord


class ReplayMixin:
    """
    Mixin that provides a standardized 'Nochmal spielen' button.
    Subclasses must implement the async method _do_replay(interaction).
    """

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self.playing = False

    def _can_replay(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="🔄 Nochmal spielen", style=discord.ButtonStyle.success, row=1)
    async def play_again(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._can_replay(interaction):
            await interaction.response.send_message(
                "Nur der Spieler darf den Button benutzen.",
                ephemeral=True
            )
            return

        if getattr(self, "playing", False):
            await interaction.response.send_message(
                "Bitte warte, bis die aktuelle Runde fertig ist.",
                ephemeral=True
            )
            return

        self.playing = True
        try:
            await self._do_replay(interaction)
        finally:
            self.playing = False

    async def _do_replay(self, interaction: discord.Interaction) -> None:
        """Must be implemented by subclasses."""
        raise NotImplementedError("Subclasses must implement _do_replay")
