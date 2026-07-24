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
        clear_manageable_roles,
        dedupe_roles_by_name,
    )
    from cogs.backup_restore_cmd import register_backup_restore
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
        clear_manageable_roles,
        dedupe_roles_by_name,
    )
    from .backup_restore_cmd import register_backup_restore

BACKUP_DB_PATH = os.getenv("BACKUP_DATA_PATH", "data/backup.db")
ATTACHMENTS_DIR = os.getenv("BACKUP_ATTACHMENTS_PATH", "data/backups/attachments")


class MessageRestoreConfirmView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=90)
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
        await asyncio.sleep(2)  # nach tree.sync der anderen Commands
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

        original_clear = cog._clear_guild_structure

        async def clear_with_roles(guild: discord.Guild) -> tuple[int, int]:
            dc, _ = await original_clear(guild)
            dr = await clear_manageable_roles(guild)
            return dc, dr

        original_restore = cog._restore_structure

        async def restore_with_extras(
            guild: discord.Guild,
            data: dict[str, Any],
            progress_msg: discord.WebhookMessage | discord.Message,
            clear_first: bool,
        ) -> None:
            original_create_role = guild.create_role

            async def create_role_reuse(*args: Any, **kwargs: Any) -> discord.Role:
                role_name = kwargs.get("name") or (args[0] if args else None)
                if role_name:
                    existing = discord.utils.get(guild.roles, name=role_name)
                    if (
                        existing
                        and not existing.is_default()
                        and not existing.managed
                        and guild.me
                        and existing.position < guild.me.top_role.position
                    ):
                        try:
                            await existing.edit(
                                permissions=kwargs.get("permissions", existing.permissions),
                                colour=kwargs.get("colour", existing.colour),
                                hoist=kwargs.get("hoist", existing.hoist),
                                mentionable=kwargs.get("mentionable", existing.mentionable),
                                reason=kwargs.get("reason", "Backup Restore reuse"),
                            )
                        except Exception as e:
                            print(f"[Backup] Rolle reuse-edit '{role_name}': {e}")
                        print(f"[Backup] Rolle wiederverwendet: {role_name}")
                        return existing
                return await original_create_role(*args, **kwargs)

            guild.create_role = create_role_reuse  # type: ignore[method-assign]
            try:
                if clear_first:
                    extra = await clear_manageable_roles(guild)
                    print(f"[Backup] Extra Rollen-Clear: {extra}")
                await original_restore(guild, data, progress_msg, clear_first)
            finally:
                guild.create_role = original_create_role  # type: ignore[method-assign]

            try:
                await dedupe_roles_by_name(guild)
                await apply_role_hierarchy(guild, data.get("roles") or [])
            except Exception as e:
                print(f"[Backup] Hierarchie: {e}")

            try:
                await apply_guild_branding(guild, data.get("guild") or {})
            except Exception as e:
                print(f"[Backup] Branding: {e}")

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
                    g = json.loads(data_json).get("guild") or {}
                    server_name = g.get("name") or "?"
                except Exception:
                    pass
                lines.append(
                    f"**#{snap_id}** · {snap_name or 'Unbenannt'} · `{ts}`\n"
                    f"└ Server: **{server_name}** · guild `{src}`"
                )

            await interaction.followup.send(
                embed=discord.Embed(
                    title="📦 Struktur-Snapshots",
                    description="\n".join(lines),
                    color=discord.Color.blurple(),
                ),
                ephemeral=True,
            )

        async def fixed_restore_messages(
            _cog: Any,
            interaction: discord.Interaction,
            channel: Optional[discord.TextChannel] = None,
            limit: Optional[int] = None,
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

            snapshot_data: Optional[dict[str, Any]] = None
            try:
                if snapshot_id is not None:
                    snap = await load_snapshot_by_id(int(snapshot_id))
                    if snap:
                        snapshot_data = snap["data"]
                else:
                    snapshot_data = await load_latest_any()

                total = await count_messages(db_path, None)
            except Exception as e:
                await interaction.response.send_message(
                    f"❌ Fehler: `{e}`", ephemeral=True
                )
                return

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
                    source_guild_id=None,
                )
            )

        cog._load_snapshot = load_snapshot_by_id  # type: ignore[method-assign]
        cog._load_latest_snapshot_data = load_latest_any  # type: ignore[method-assign]
        cog._build_overwrites = build_overwrites  # type: ignore[method-assign]
        cog._save_snapshot = save_snapshot_with_icon  # type: ignore[method-assign]
        cog._clear_guild_structure = clear_with_roles  # type: ignore[method-assign]
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
            if cmd.name in ("backup-snapshots", "backup-restore-messages") and hasattr(
                cmd, "_callback"
            ):
                cmd._callback = (  # type: ignore[attr-defined]
                    fixed_snapshots
                    if cmd.name == "backup-snapshots"
                    else fixed_restore_messages
                )
                if cmd.name not in patched:
                    patched.append(cmd.name)

        # --- backup-restore: optional snapshot_id (leer = neuester) ---
        try:
            # Alte Variante (required snapshot_id) entfernen
            if hasattr(cog, "__cog_app_commands__"):
                cog.__cog_app_commands__[:] = [
                    c for c in cog.__cog_app_commands__ if c.name != "backup-restore"
                ]
            existing = self.bot.tree.get_command("backup-restore")
            if existing is not None:
                self.bot.tree.remove_command("backup-restore")

            new_cmd = register_backup_restore(self.bot, db_path)
            self.bot.tree.add_command(new_cmd)
            patched.append("backup-restore(optional id)")

            # Nochmal syncen, damit Discord optional sieht
            try:
                await self.bot.tree.sync()
                print("[BackupAdmin] tree.sync nach backup-restore Update")
            except Exception as e:
                print(f"[BackupAdmin] tree.sync: {e}")
        except Exception as e:
            print(f"[BackupAdmin] backup-restore replace fehlgeschlagen: {e}")

        print(f"[BackupAdmin] Patches OK: {patched}")

    # ---------- Exclude / missing ----------

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
