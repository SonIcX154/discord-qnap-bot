from __future__ import annotations

import os
import json
import time
import traceback
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
        is_guild_excluded,
        add_excluded_guild,
        remove_excluded_guild,
        list_excluded_guilds,
    )
    from utils.message_restore import count_messages
    from utils.structure_helpers import (
        build_overwrites,
        apply_role_hierarchy,
        apply_guild_branding,
        fetch_icon_b64,
        clear_manageable_roles,
        clear_channels,
        dedupe_roles_by_name,
        convert_to_news_channels,
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
        is_guild_excluded,
        add_excluded_guild,
        remove_excluded_guild,
        list_excluded_guilds,
    )
    from ..utils.message_restore import count_messages
    from ..utils.structure_helpers import (
        build_overwrites,
        apply_role_hierarchy,
        apply_guild_branding,
        fetch_icon_b64,
        clear_manageable_roles,
        clear_channels,
        dedupe_roles_by_name,
        convert_to_news_channels,
    )
    from .backup_restore_cmd import register_backup_restore

BACKUP_DB_PATH = os.getenv("BACKUP_DATA_PATH", "data/backup.db")
ATTACHMENTS_DIR = os.getenv("BACKUP_ATTACHMENTS_PATH", "data/backups/attachments")
PROGRESS_CHANNEL_NAME = "backup-restore-progress"


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
        await asyncio.sleep(2)
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

        original_store = cog._store_message

        async def store_filtered(
            message: discord.Message, *, is_edit: bool = False
        ) -> None:
            if message.guild and await is_guild_excluded(db_path, message.guild.id):
                return
            task = getattr(cog, "_msg_restore_task", None)
            if task is not None and not task.done():
                return
            await original_store(message, is_edit=is_edit)

        cog._store_message = store_filtered  # type: ignore[method-assign]

        original_restore = cog._restore_structure

        async def restore_with_extras(
            guild: discord.Guild,
            data: dict[str, Any],
            progress_msg: discord.WebhookMessage | discord.Message,
            clear_first: bool,
        ) -> None:
            print(f"[Backup] restore_with_extras START clear_first={clear_first} guild={guild.id}")

            progress_channel: Optional[discord.TextChannel] = None
            current_progress = progress_msg

            async def bump(text: str, *, done: bool = False, error: bool = False) -> None:
                print(f"[Backup] progress: {text}")
                color = discord.Color.green() if done else (
                    discord.Color.red() if error else discord.Color.orange()
                )
                title = (
                    "✅ Struktur-Restore abgeschlossen" if done
                    else ("❌ Restore fehlgeschlagen" if error else "🔄 Struktur-Restore läuft...")
                )
                try:
                    await current_progress.edit(
                        embed=discord.Embed(title=title, description=text, color=color),
                        view=None,
                    )
                except Exception as e:
                    print(f"[Backup] progress edit fail: {e}")

            async def cleanup_progress_channel() -> None:
                if progress_channel is None:
                    return
                try:
                    await bump(
                        "Fertig. Dieser Channel wird in **10 Sekunden** gelöscht…",
                        done=True,
                    )
                except Exception:
                    pass
                await asyncio.sleep(10)
                try:
                    await progress_channel.delete(reason="Backup Restore progress cleanup")
                    print("[Backup] Progress-Channel gelöscht")
                except Exception as e:
                    print(f"[Backup] Progress-Channel löschen: {e}")

            try:
                if clear_first:
                    try:
                        for ch in list(guild.text_channels):
                            if ch.name == PROGRESS_CHANNEL_NAME:
                                try:
                                    await ch.delete(reason="Backup Restore: alter Progress-Channel")
                                except Exception:
                                    pass

                        progress_channel = await guild.create_text_channel(
                            PROGRESS_CHANNEL_NAME,
                            reason="Backup Restore progress",
                            topic="Temporärer Fortschritts-Channel – wird nach dem Restore gelöscht",
                        )
                        print(f"[Backup] Progress-Channel erstellt: #{progress_channel.name}")

                        current_progress = await progress_channel.send(
                            embed=discord.Embed(
                                title="🔄 Struktur-Restore läuft...",
                                description="Progress-Channel bereit. Starte Clear…",
                                color=discord.Color.orange(),
                            )
                        )

                        try:
                            await progress_msg.edit(
                                embed=discord.Embed(
                                    title="🔄 Struktur-Restore",
                                    description=(
                                        f"Fortschritt läuft in **#{progress_channel.name}**\n"
                                        "(wird nach dem Restore automatisch gelöscht)"
                                    ),
                                    color=discord.Color.orange(),
                                ),
                                view=None,
                            )
                        except Exception:
                            pass
                    except Exception as e:
                        print(f"[Backup] Progress-Channel anlegen fehlgeschlagen: {e}")
                        progress_channel = None
                        current_progress = progress_msg

                await bump("Starte…")

                if clear_first:
                    keep_id = progress_channel.id if progress_channel else None
                    await bump("Lösche Channels…")
                    dc = await clear_channels(guild, keep_channel_id=keep_id)
                    await bump(f"Channels gelöscht (**{dc}**). Lösche Rollen…")
                    dr = await clear_manageable_roles(guild)
                    await bump(f"Rollen gelöscht (**{dr}**). Erstelle Struktur…")
                else:
                    await bump("Erstelle Struktur (ohne Clear)…")

                print("[Backup] rufe original_restore (clear_first=False)…")
                await original_restore(guild, data, current_progress, False)
                print("[Backup] original_restore fertig")

                await bump("Announcement-Channels…")
                try:
                    n = await convert_to_news_channels(guild, data.get("channels") or [])
                    if n:
                        print(f"[Backup] {n} Channel(s) → News/Announcement")
                except Exception as e:
                    print(f"[Backup] News-Channel convert: {e}")

                await bump("Setze Rollen-Hierarchie…")
                try:
                    await dedupe_roles_by_name(guild)
                    await apply_role_hierarchy(guild, data.get("roles") or [])
                except Exception as e:
                    print(f"[Backup] Hierarchie: {e}")
                    traceback.print_exc()

                await bump("Server-Name/Icon…")
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

                await bump("Alles erledigt.", done=True)
                print("[Backup] restore_with_extras DONE")

            except Exception as e:
                print(f"[Backup] restore_with_extras CRASH: {e}")
                traceback.print_exc()
                try:
                    await bump(f"`{e}`", error=True)
                except Exception:
                    pass
            finally:
                cog._restore_task = None
                if progress_channel is not None:
                    try:
                        await cleanup_progress_channel()
                    except Exception as e:
                        print(f"[Backup] cleanup progress: {e}")

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
                    "Webhook · Name • Timestamp · Avatar · Mentions aus."
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
        cog._restore_structure = restore_with_extras  # type: ignore[method-assign]

        patched = ["guild-exclude+restore-mute", "news-channels"]
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

        try:
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

            try:
                await self.bot.tree.sync()
                print("[BackupAdmin] tree.sync nach backup-restore Update")
            except Exception as e:
                print(f"[BackupAdmin] tree.sync: {e}")
        except Exception as e:
            print(f"[BackupAdmin] backup-restore replace fehlgeschlagen: {e}")

        print(f"[BackupAdmin] Patches OK: {patched}")

    # ---------- Exclude guild / channel / missing ----------

    @app_commands.command(
        name="backup-exclude-guild",
        description="Diesen Server vom Message-Logging/Backfill ausschließen",
    )
    @app_commands.describe(reason="Optionaler Grund")
    @app_commands.default_permissions(administrator=True)
    async def backup_exclude_guild(
        self,
        interaction: discord.Interaction,
        reason: Optional[str] = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Nur auf einem Server.", ephemeral=True)
            return
        await add_excluded_guild(
            self.db_path,
            interaction.guild.id,
            reason or "Restore-Server",
        )
        await interaction.response.send_message(
            f"✅ Server **{interaction.guild.name}** (`{interaction.guild.id}`) "
            f"wird nicht mehr geloggt/backfilled.\n"
            f"Webhooks & normale Server bleiben unberührt.\n"
            f"Wieder aktivieren: `/backup-include-guild`",
            ephemeral=True,
        )

    @app_commands.command(
        name="backup-include-guild",
        description="Server wieder ins Message-Logging aufnehmen",
    )
    @app_commands.default_permissions(administrator=True)
    async def backup_include_guild(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Nur auf einem Server.", ephemeral=True)
            return
        ok = await remove_excluded_guild(self.db_path, interaction.guild.id)
        msg = (
            f"✅ **{interaction.guild.name}** ist wieder im Backup."
            if ok
            else f"ℹ️ **{interaction.guild.name}** war nicht excluded."
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(
        name="backup-excluded-guilds",
        description="Listet vom Backup ausgeschlossene Server",
    )
    @app_commands.default_permissions(administrator=True)
    async def backup_excluded_guilds(self, interaction: discord.Interaction) -> None:
        rows = await list_excluded_guilds(self.db_path)
        if not rows:
            await interaction.response.send_message(
                "Keine excluded Guilds.", ephemeral=True
            )
            return
        lines = []
        for gid, reason in rows:
            g = self.bot.get_guild(gid)
            name = g.name if g else f"`{gid}`"
            extra = f" – {reason}" if reason else ""
            lines.append(f"• **{name}** (`{gid}`){extra}")
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🚫 Excluded Guilds",
                description="\n".join(lines),
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )

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
