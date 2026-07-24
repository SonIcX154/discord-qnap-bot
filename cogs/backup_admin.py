from __future__ import annotations

import os
import json
import asyncio
import aiosqlite
import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional, Any
from datetime import datetime, timezone

try:
    from utils.backup_ops import (
        add_excluded_channel,
        remove_excluded_channel,
        list_excluded_channels,
        download_missing_attachments,
        ensure_extra_tables,
        save_channel_id_map,
    )
    from utils.message_restore import count_messages, run_message_restore
except ImportError:
    from ..utils.backup_ops import (
        add_excluded_channel,
        remove_excluded_channel,
        list_excluded_channels,
        download_missing_attachments,
        ensure_extra_tables,
        save_channel_id_map,
    )
    from ..utils.message_restore import count_messages, run_message_restore

BACKUP_DB_PATH = os.getenv("BACKUP_DATA_PATH", "data/backup.db")
ATTACHMENTS_DIR = os.getenv("BACKUP_ATTACHMENTS_PATH", "data/backups/attachments")


class _AlwaysTruthyDict(dict):
    def __bool__(self) -> bool:
        return True


class CrossRestoreConfirmView(discord.ui.View):
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


class MessageRestoreConfirmView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=60)
        self.confirmed = False

    @discord.ui.button(label="✅ Nachrichten wiederherstellen", style=discord.ButtonStyle.danger)
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
            content="Nachrichten-Restore abgebrochen.", embed=None, view=None
        )
        self.stop()


class BackupAdminCog(commands.Cog):
    """Admin + Single-Guild Disaster-Recovery (eine Instanz = eine Guild)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db_path = BACKUP_DB_PATH
        self._dl_task: Optional[asyncio.Task] = None

    async def cog_load(self) -> None:
        await ensure_extra_tables(self.db_path)
        self.bot.loop.create_task(self._patch_backup_cog())

    async def _patch_backup_cog(self) -> None:
        await asyncio.sleep(1)
        cog = self.bot.get_cog("BackupCog")
        if cog is None:
            print("[BackupAdmin] BackupCog nicht gefunden – Patch übersprungen")
            return

        db_path = self.db_path

        async def load_snapshot_by_id(
            snapshot_id: int, guild_id: Optional[int] = None
        ) -> Optional[dict[str, Any]]:
            async with aiosqlite.connect(db_path) as db:
                async with db.execute(
                    "SELECT name, data, guild_id FROM snapshots WHERE id = ?",
                    (snapshot_id,),
                ) as cur:
                    row = await cur.fetchone()
            if not row:
                return None
            return {
                "name": row[0],
                "data": json.loads(row[1]),
                "source_guild_id": int(row[2]),
            }

        async def load_latest_any(guild_id: Optional[int] = None) -> Optional[dict[str, Any]]:
            async with aiosqlite.connect(db_path) as db:
                async with db.execute(
                    "SELECT data FROM snapshots ORDER BY created_at DESC LIMIT 1"
                ) as cur:
                    row = await cur.fetchone()
            if not row:
                return None
            return json.loads(row[0])

        def fixed_build_overwrites(
            guild: discord.Guild,
            overwrites_data: list[dict[str, Any]],
            role_map: dict[int, int],
        ) -> dict:
            result: _AlwaysTruthyDict = _AlwaysTruthyDict()
            for ow in overwrites_data:
                target: Optional[discord.abc.Snowflake] = None
                if ow.get("type") == "role":
                    old_id = ow["id"]
                    mapped = role_map.get(old_id)
                    if old_id == guild.id or mapped == guild.default_role.id:
                        target = guild.default_role
                    elif mapped:
                        target = guild.get_role(mapped)
                elif ow.get("type") == "member":
                    member = guild.get_member(ow["id"])
                    if member:
                        target = member
                if target is None:
                    continue
                result[target] = discord.PermissionOverwrite.from_pair(
                    discord.Permissions(ow.get("allow", 0)),
                    discord.Permissions(ow.get("deny", 0)),
                )
            return result

        # --- /backup-snapshots: alle Snapshots der Instanz ---
        async def fixed_snapshots(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            async with aiosqlite.connect(db_path) as db:
                async with db.execute(
                    """
                    SELECT id, name, created_at, guild_id
                    FROM snapshots ORDER BY created_at DESC LIMIT 25
                    """
                ) as cur:
                    rows = await cur.fetchall()
            if not rows:
                await interaction.followup.send(
                    "Noch keine Snapshots. Nutze `/backup-snapshot`.", ephemeral=True
                )
                return
            lines = []
            for snap_id, name, created_at, src in rows:
                ts = datetime.fromtimestamp(created_at, tz=timezone.utc).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
                lines.append(
                    f"**#{snap_id}** · {name or 'Unbenannt'} · `{ts}` · guild `{src}`"
                )
            embed = discord.Embed(
                title="📦 Struktur-Snapshots (Instanz)",
                description="\n".join(lines),
                color=discord.Color.blurple(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

        # --- /backup-restore-messages: Nachrichten instanz-weit ---
        async def fixed_restore_messages(
            interaction: discord.Interaction,
            channel: Optional[discord.TextChannel] = None,
            limit: Optional[app_commands.Range[int, 1, 10000]] = None,
            match_by_name: bool = True,
            snapshot_id: Optional[int] = None,
        ) -> None:
            if not interaction.guild:
                await interaction.response.send_message(
                    "Nur auf einem Server nutzbar.", ephemeral=True
                )
                return

            if cog._msg_restore_task and not cog._msg_restore_task.done():
                await interaction.response.send_message(
                    "⚠️ Ein Nachrichten-Restore läuft bereits.", ephemeral=True
                )
                return

            # Snapshot für Name-Mapping + source_guild_id
            snapshot_data: Optional[dict[str, Any]] = None
            source_guild_id: Optional[int] = None
            if snapshot_id is not None:
                snap = await load_snapshot_by_id(snapshot_id)
                if snap:
                    snapshot_data = snap["data"]
                    source_guild_id = snap.get("source_guild_id")
            else:
                snapshot_data = await load_latest_any()
                if snapshot_data:
                    gid = (snapshot_data.get("guild") or {}).get("id")
                    if gid:
                        source_guild_id = int(gid)

            # Instanz-weit zählen (nach Rebuild ist interaction.guild.id neu)
            total = await count_messages(db_path, source_guild_id)
            if total == 0:
                total = await count_messages(db_path, None)
            if total == 0:
                await interaction.response.send_message(
                    "Keine gespeicherten Nachrichten in der Backup-DB.",
                    ephemeral=True,
                )
                return

            scope = f"nur **#{channel.name}**" if channel else "**alle Channels**"
            limit_txt = f"max. **{limit}**/Channel" if limit else "**alle** Nachrichten"

            embed = discord.Embed(
                title="⚠️ Nachrichten-Restore bestätigen",
                description=(
                    f"Gespeicherte Nachrichten: **{total:,}**\n"
                    f"Ziel: {scope}\n"
                    f"Limit: {limit_txt}\n"
                    f"Name-Match: **{'an' if match_by_name else 'aus'}**\n\n"
                    "Daten kommen aus der **Instanz-DB** (auch nach Server-Rebuild).\n"
                    "Webhook · Original-Name/Avatar · Mentions aus."
                ),
                color=discord.Color.orange(),
            )

            view = MessageRestoreConfirmView()
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            await view.wait()
            if not view.confirmed:
                return

            progress_msg = await interaction.followup.send(
                embed=discord.Embed(
                    title="🔄 Nachrichten-Restore startet...",
                    color=discord.Color.orange(),
                ),
                ephemeral=False,
            )

            cog._msg_restore_task = asyncio.create_task(
                cog._run_message_restore_task(
                    guild=interaction.guild,
                    db_path=db_path,
                    progress_msg=progress_msg,
                    channel_filter=channel,
                    limit_per_channel=int(limit) if limit else None,
                    match_by_name=match_by_name,
                    snapshot_data=snapshot_data,
                    source_guild_id=source_guild_id,
                )
            )

        cog._load_snapshot = load_snapshot_by_id  # type: ignore[method-assign]
        cog._load_latest_snapshot_data = load_latest_any  # type: ignore[method-assign]
        cog._build_overwrites = fixed_build_overwrites  # type: ignore[method-assign]

        # Callbacks der App-Commands austauschen
        for cmd in getattr(cog, "__cog_app_commands__", []):
            if cmd.name == "backup-snapshots":
                cmd._callback = fixed_snapshots  # type: ignore[attr-defined]
            elif cmd.name == "backup-restore-messages":
                cmd._callback = fixed_restore_messages  # type: ignore[attr-defined]

        # Tree-Commands (falls schon registriert)
        for cmd in self.bot.tree.get_commands():
            if cmd.name == "backup-snapshots":
                cmd.callback = fixed_snapshots  # type: ignore[attr-defined]
            elif cmd.name == "backup-restore-messages":
                cmd.callback = fixed_restore_messages  # type: ignore[attr-defined]

        print(
            "[BackupAdmin] Patches: Snapshots/Messages instanz-weit · overwrites-dict · ID-Lookup"
        )

    async def _load_snapshot_any(self, snapshot_id: int) -> Optional[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT name, data, guild_id FROM snapshots WHERE id = ?",
                (snapshot_id,),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        return {
            "name": row[0],
            "data": json.loads(row[1]),
            "source_guild_id": int(row[2]),
        }

    # ---------- Exclude ----------

    @app_commands.command(
        name="backup-exclude",
        description="Channel vom Message-Logging/Backfill ausschließen",
    )
    @app_commands.describe(channel="Channel", reason="Optionaler Grund")
    @app_commands.default_permissions(administrator=True)
    async def backup_exclude(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        reason: Optional[str] = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Nur auf einem Server.", ephemeral=True)
            return
        await add_excluded_channel(self.db_path, channel.id, interaction.guild.id, reason)
        await interaction.response.send_message(
            f"✅ **#{channel.name}** wird ab jetzt nicht mehr geloggt/backfilled.",
            ephemeral=True,
        )

    @app_commands.command(
        name="backup-unexclude",
        description="Channel wieder in Backup aufnehmen",
    )
    @app_commands.describe(channel="Channel")
    @app_commands.default_permissions(administrator=True)
    async def backup_unexclude(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        ok = await remove_excluded_channel(self.db_path, channel.id)
        msg = (
            f"✅ **#{channel.name}** ist wieder im Backup."
            if ok
            else f"ℹ️ **#{channel.name}** war nicht excluded."
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="backup-excludes", description="Listet ausgeschlossene Channels")
    @app_commands.default_permissions(administrator=True)
    async def backup_excludes(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Nur auf einem Server.", ephemeral=True)
            return
        rows = await list_excluded_channels(self.db_path, interaction.guild.id)
        if not rows:
            await interaction.response.send_message("Keine excluded Channels.", ephemeral=True)
            return
        lines = []
        for cid, reason in rows:
            ch = interaction.guild.get_channel(cid)
            name = f"#{ch.name}" if ch else f"`{cid}`"
            extra = f" – {reason}" if reason else ""
            lines.append(f"• {name}{extra}")
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🚫 Excluded Channels",
                description="\n".join(lines),
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="backup-snapshots-all",
        description="Listet alle Struktur-Snapshots dieser Bot-Instanz",
    )
    @app_commands.default_permissions(administrator=True)
    async def backup_snapshots_all(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT id, name, created_at, guild_id
                FROM snapshots ORDER BY created_at DESC LIMIT 25
                """
            ) as cur:
                rows = await cur.fetchall()
        if not rows:
            await interaction.followup.send("Keine Snapshots in der DB.", ephemeral=True)
            return
        lines = []
        for snap_id, name, created_at, src_guild in rows:
            ts = datetime.fromtimestamp(created_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            lines.append(
                f"**#{snap_id}** · {name or 'Unbenannt'} · `{ts}` · alte guild_id: `{src_guild}`"
            )
        embed = discord.Embed(
            title="📦 Struktur-Snapshots (Instanz)",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="backup-restore-cross",
        description="Struktur neu aufbauen (auch nach Server-Löschung / neuer Guild-ID)",
    )
    @app_commands.describe(
        snapshot_id="Snapshot-ID",
        clear_first="Bestehende Channels/Rollen vorher löschen",
    )
    @app_commands.default_permissions(administrator=True)
    async def backup_restore_cross(
        self,
        interaction: discord.Interaction,
        snapshot_id: int,
        clear_first: bool = False,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Nur auf einem Server.", ephemeral=True)
            return

        backup_cog = self.bot.get_cog("BackupCog")
        if backup_cog is None:
            await interaction.response.send_message("❌ BackupCog nicht geladen.", ephemeral=True)
            return

        if getattr(backup_cog, "_restore_task", None) and not backup_cog._restore_task.done():
            await interaction.response.send_message("⚠️ Ein Restore läuft bereits.", ephemeral=True)
            return

        snap = await self._load_snapshot_any(snapshot_id)
        if not snap:
            await interaction.response.send_message(
                f"❌ Snapshot **#{snapshot_id}** nicht in der DB.",
                ephemeral=True,
            )
            return

        data = snap["data"]
        role_count = len([r for r in data.get("roles", []) if not r.get("managed")])
        cat_count = len(data.get("categories", []))
        ch_count = len(data.get("channels", []))
        src = snap["source_guild_id"]

        warning = "\n\n⚠️ **clear_first**: Channels/Rollen werden gelöscht." if clear_first else ""

        embed = discord.Embed(
            title="⚠️ Struktur-Restore (Disaster Recovery)",
            description=(
                f"Snapshot **#{snapshot_id}** – {snap['name'] or 'Unbenannt'}\n"
                f"Alte guild_id: `{src}` → Ziel: **{interaction.guild.name}**\n\n"
                f"Rollen: **{role_count}** · Kategorien: **{cat_count}** · Channels: **{ch_count}**"
                f"{warning}"
            ),
            color=discord.Color.orange(),
        )

        view = CrossRestoreConfirmView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        await view.wait()
        if not view.confirmed:
            return

        progress_msg = await interaction.followup.send(
            embed=discord.Embed(title="🔄 Struktur-Restore startet…", color=discord.Color.orange()),
            ephemeral=False,
        )

        async def wrapped(guild, data, progress_msg, clear_first):
            await backup_cog._restore_structure(guild, data, progress_msg, clear_first)
            mapping: dict[int, int] = {}
            for ch_data in list(data.get("channels", [])) + list(data.get("categories", [])):
                old_id = ch_data.get("id")
                name = ch_data.get("name")
                if not old_id or not name:
                    continue
                for c in guild.channels:
                    if c.name == name:
                        mapping[int(old_id)] = c.id
                        break
            if mapping:
                await save_channel_id_map(self.db_path, guild.id, mapping)
                print(f"[Backup] channel_id_map: {len(mapping)} Einträge")

        backup_cog._restore_task = asyncio.create_task(
            wrapped(interaction.guild, data, progress_msg, clear_first)
        )

    @app_commands.command(
        name="backup-download-missing",
        description="Lädt fehlende Attachments über gespeicherte CDN-URLs nach",
    )
    @app_commands.describe(limit="Max. Nachrichten (Default 500)")
    @app_commands.default_permissions(administrator=True)
    async def backup_download_missing(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 5000] = 500,
    ) -> None:
        if self._dl_task and not self._dl_task.done():
            await interaction.response.send_message("⚠️ Download läuft bereits.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)
        progress = await interaction.followup.send(
            embed=discord.Embed(
                title="⬇️ Missing Attachments…",
                description="Starte Download…",
                color=discord.Color.orange(),
            )
        )

        async def run() -> None:
            try:
                async def on_progress(processed: int, downloaded: int, failed: int) -> None:
                    try:
                        await progress.edit(
                            embed=discord.Embed(
                                title="⬇️ Missing Attachments…",
                                description=(
                                    f"Geprüft: **{processed}**\n"
                                    f"Neu: **{downloaded}** · Fehler: **{failed}**"
                                ),
                                color=discord.Color.orange(),
                            )
                        )
                    except Exception:
                        pass

                stats = await download_missing_attachments(
                    self.db_path,
                    ATTACHMENTS_DIR,
                    guild_id=None,
                    limit=int(limit),
                    on_progress=on_progress,
                )
                embed = discord.Embed(
                    title="✅ Missing Attachments fertig",
                    color=discord.Color.green(),
                )
                for k, label in [
                    ("checked", "Geprüft"),
                    ("downloaded", "Neu geladen"),
                    ("already_ok", "Bereits OK"),
                    ("failed", "Fehler"),
                    ("updated_rows", "DB-Updates"),
                ]:
                    embed.add_field(name=label, value=str(stats[k]), inline=True)
                await progress.edit(embed=embed)
            except Exception as e:
                await progress.edit(
                    embed=discord.Embed(
                        title="❌ Download fehlgeschlagen",
                        description=f"`{e}`",
                        color=discord.Color.red(),
                    )
                )
            finally:
                self._dl_task = None

        self._dl_task = asyncio.create_task(run())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BackupAdminCog(bot))
