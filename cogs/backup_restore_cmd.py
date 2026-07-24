"""backup-restore with optional snapshot_id (defaults to latest)."""
from __future__ import annotations

import json
import asyncio
import aiosqlite
import discord
from discord import app_commands
from typing import Optional, Any


class RestoreConfirmView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=60)
        self.confirmed = False

    @discord.ui.button(label="✅ Wiederherstellen", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.confirmed = True
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="❌ Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.confirmed = False
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(
            content="Restore abgebrochen.", embed=None, view=None
        )
        self.stop()


def register_backup_restore(bot, db_path: str) -> app_commands.Command:
    """Create backup-restore command with optional snapshot_id."""

    @app_commands.command(
        name="backup-restore",
        description="Stellt die Server-Struktur aus einem Snapshot wieder her",
    )
    @app_commands.describe(
        snapshot_id="Snapshot-ID (leer = neuester Snapshot)",
        clear_first="Vorher Channels und (nicht-managed) Rollen löschen",
    )
    @app_commands.default_permissions(administrator=True)
    async def backup_restore(
        interaction: discord.Interaction,
        snapshot_id: Optional[int] = None,
        clear_first: bool = False,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Nur auf einem Server nutzbar.", ephemeral=True)
            return

        cog = bot.get_cog("BackupCog")
        if cog is None:
            await interaction.response.send_message("❌ BackupCog nicht geladen.", ephemeral=True)
            return

        if getattr(cog, "_restore_task", None) and not cog._restore_task.done():
            await interaction.response.send_message("⚠️ Ein Restore läuft bereits.", ephemeral=True)
            return

        resolved_id: Optional[int] = snapshot_id

        async with aiosqlite.connect(db_path) as db:
            if resolved_id is not None:
                async with db.execute(
                    "SELECT id, name, data FROM snapshots WHERE id = ?",
                    (resolved_id,),
                ) as cur:
                    row = await cur.fetchone()
            else:
                async with db.execute(
                    "SELECT id, name, data FROM snapshots ORDER BY created_at DESC LIMIT 1"
                ) as cur:
                    row = await cur.fetchone()

        if not row:
            await interaction.response.send_message(
                "❌ Kein Snapshot gefunden. Nutze `/backup-snapshot` oder gib eine ID an.",
                ephemeral=True,
            )
            return

        resolved_id = int(row[0])
        snap_name = row[1] or "Unbenannt"
        data: dict[str, Any] = json.loads(row[2])

        role_count = len([r for r in data.get("roles", []) if not r.get("managed")])
        cat_count = len(data.get("categories", []))
        ch_count = len(data.get("channels", []))

        warning = (
            "\n\n⚠️ **clear_first**: Bestehende Channels/Rollen werden gelöscht."
            if clear_first
            else ""
        )

        embed = discord.Embed(
            title="⚠️ Struktur-Restore bestätigen",
            description=(
                f"Snapshot **#{resolved_id}** – {snap_name}\n\n"
                f"Rollen: **{role_count}** · Kategorien: **{cat_count}** · Channels: **{ch_count}**"
                f"{warning}"
            ),
            color=discord.Color.orange(),
        )

        view = RestoreConfirmView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        await view.wait()

        if not view.confirmed:
            return

        progress_msg = await interaction.followup.send(
            embed=discord.Embed(
                title="🔄 Struktur-Restore startet...",
                color=discord.Color.orange(),
            ),
            ephemeral=False,
        )

        cog._restore_task = asyncio.create_task(
            cog._restore_structure(interaction.guild, data, progress_msg, clear_first)
        )

    return backup_restore
