from __future__ import annotations

import os
import json
import time
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
    from utils.message_restore import count_messages
    from utils.structure_helpers import (
        build_overwrites,
        apply_role_hierarchy,
        apply_guild_branding,
        fetch_icon_b64,
    )
except ImportError:
    from ..utils.backup_ops import (
        add_excluded_channel,
        remove_excluded_channel,
        list_excluded_channels,
        download_missing_attachments,
        ensure_extra_tables,
        save_channel_id_map,
    )
    from ..utils.message_restore import count_messages
    from ..utils.structure_helpers import (
        build_overwrites,
        apply_role_hierarchy,
        apply_guild_branding,
        fetch_icon_b64,
    )

BACKUP_DB_PATH = os.getenv("BACKUP_DATA_PATH", "data/backup.db")
ATTACHMENTS_DIR = os.getenv("BACKUP_ATTACHMENTS_PATH", "data/backups/attachments")


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
    """Admin-Hilfen + Patches für Single-Guild Disaster-Recovery."""

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

        original_save = cog._save_snapshot

        async def save_snapshot_with_icon(
            guild: discord.Guild,
            name: Optional[str],
            created_by: int,
        ) -> int:
            data = cog._build_structure_snapshot(guild)
            icon_b64 = await fetch_icon_b64(guild)
            if icon_b64:
                data.setdefault("guild", {})["icon_b64"] = icon_b64
            data_json = json.dumps(data, ensure_ascii=False)
            if not name:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                name = f"Snapshot {ts}"
            async with aiosqlite.connect(db_path) as db:
                cursor = await db.execute(
                    """
                    INSERT INTO snapshots (guild_id, name, created_at, created_by, data)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (guild.id, name, int(time.time()), created_by, data_json),
                )
                await db.commit()
                return cursor.lastrowid  # type: ignore[return-value]

        original_restore = cog._restore_structure

        async def restore_with_extras(
            guild: discord.Guild,
            data: dict[str, Any],
            progress_msg: discord.WebhookMessage | discord.Message,
            clear_first: bool,
        ) -> None:
            await original_restore(guild, data, progress_msg, clear_first)

            # Hierarchie korrigieren (Reihenfolge wie Snapshot, unter Bot-Rolle)
            try:
                await apply_role_hierarchy(guild, data.get("roles") or [])
            except Exception as e:
                print(f"[Backup] Hierarchie-Nachbearbeitung: {e}")

            # Name + Icon
            try:
                await apply_guild_branding(guild, data.get("guild") or {})
            except Exception as e:
                print(f"[Backup] Branding-Nachbearbeitung: {e}")

            # Channel-Map für Message-Restore
            try:
                mapping: dict[int, int] = {}
                for ch_data in list(data.get("channels", [])) + list(
                    data.get("categories", [])
                ):
                    old_id = ch_data.get("id")
                    ch_name = ch_data.get("name")
                    if not old_id or not ch_name:
                        continue
                    for c in guild.channels:
                        if c.name == ch_name:
                            mapping[int(old_id)] = c.id
                            break
                if mapping:
                    await save_channel_id_map(db_path, guild.id, mapping)
                    print(f"[Backup] channel_id_map: {len(mapping)} Einträge")
            except Exception as e:
                print(f"[Backup] channel_id_map: {e}")

        async def fixed_snapshots(
            _cog: Any, interaction: discord.Interaction
        ) -> None:
            await interaction.response.defer(ephemeral=True)
            try:
                async with aiosqlite.connect(db_path) as db:
                    async with db.execute(
                        """
                        SELECT id, name, created_at, guild_id, data
                        FROM snapshots ORDER BY created_at DESC LIMIT 25
                        """
                    ) as cur:
                        rows = await cur.fetchall()
            except Exception as e:
                await interaction.followup.send(f"❌ DB-Fehler: `{e}`", ephemeral=True)
                return

            if not rows:
                await interaction.followup.send(
                    "Noch keine Snapshots. Nutze `/backup-snapshot`.", ephemeral=True
                )
                return

            lines = []
            for snap_id, snap_name, created_at, src, data_json in rows:
                ts = datetime.fromtimestamp(created_at, tz=timezone.utc).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
                server_name = "?"
                try:
                    g = (json.loads(data_json).get("guild") or {})
                    server_name = g.get("name") or "?"
                except Exception:
                    pass
                lines.append(
                    f"**#{snap_id}** · {snap_name or 'Unbenannt'} · `{ts}`\n"
                    f"└ Server: **{server_name}** · guild `{src}`"
                )

            embed = discord.Embed(
                title="📦 Struktur-Snapshots",
                description="\n".join(lines),
                color=discord.Color.blurple(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

        async def fixed_restore_messages(
            _cog: Any,
            interaction: discord.Interaction,
            channel: Optional[discord.TextChannel] = None,
            limit: Optional[int] = None,
            match_by_name: bool = True,
            snapshot_id: Optional[int] = None,
        ) -> None:
            await interaction.response.defer(ephemeral=True)

            if not interaction.guild:
                await interaction.followup.send("Nur auf einem Server nutzbar.", ephemeral=True)
                return

            if cog._msg_restore_task and not cog._msg_restore_task.done():
                await interaction.followup.send(
                    "⚠️ Ein Nachrichten-Restore läuft bereits.", ephemeral=True
                )
                return

            snapshot_data: Optional[dict[str, Any]] = None
            source_guild_id: Optional[int] = None
            try:
                if snapshot_id is not None:
                    snap = await load_snapshot_by_id(int(snapshot_id))
                    if snap:
                        snapshot_data = snap["data"]
                        source_guild_id = snap.get("source_guild_id")
                else:
                    snapshot_data = await load_latest_any()
                    if snapshot_data:
                        gid = (snapshot_data.get("guild") or {}).get("id")
                        if gid:
                            source_guild_id = int(gid)

                total = await count_messages(db_path, source_guild_id)
                if total == 0:
                    total = await count_messages(db_path, None)
            except Exception as e:
                await interaction.followup.send(f"❌ Fehler: `{e}`", ephemeral=True)
                return

            if total == 0:
                await interaction.followup.send(
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
                    "Webhook · Original-Name/Avatar · Mentions aus."
                ),
                color=discord.Color.orange(),
            )

            view = MessageRestoreConfirmView()
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
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
        cog._build_overwrites = build_overwrites  # type: ignore[method-assign]
        cog._save_snapshot = save_snapshot_with_icon  # type: ignore[method-assign]
        cog._restore_structure = restore_with_extras  # type: ignore[method-assign]

        patched = []
        for cmd in getattr(cog, "__cog_app_commands__", []):
            if cmd.name == "backup-snapshots":
                cmd._callback = fixed_snapshots  # type: ignore[attr-defined]
                patched.append("backup-snapshots")
            elif cmd.name == "backup-restore-messages":
                cmd._callback = fixed_restore_messages  # type: ignore[attr-defined]
                patched.append("backup-restore-messages")

        for cmd in self.bot.tree.get_commands():
            if cmd.name in ("backup-snapshots", "backup-restore-messages"):
                if hasattr(cmd, "_callback"):
                    cmd._callback = (  # type: ignore[attr-defined]
                        fixed_snapshots
                        if cmd.name == "backup-snapshots"
                        else fixed_restore_messages
                    )
                    if cmd.name not in patched:
                        patched.append(cmd.name)

        print(
            f"[BackupAdmin] Patches: hierarchy+branding+icon · commands {patched}"
        )

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
