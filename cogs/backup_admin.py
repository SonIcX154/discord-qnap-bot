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
        add_excluded_guild,
        remove_excluded_guild,
        list_excluded_guilds,
        purge_soft_deleted,
        purge_all_soft_deleted,
        purge_excluded_messages,
        count_purge_candidates,
        prune_channel_progress,
        SOFT_DELETE_RETENTION_DAYS,
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
except ImportError:
    from ..utils.backup_ops import (
        add_excluded_channel,
        remove_excluded_channel,
        list_excluded_channels,
        download_missing_attachments,
        ensure_extra_tables,
        save_channel_id_map,
        add_excluded_guild,
        remove_excluded_guild,
        list_excluded_guilds,
        purge_soft_deleted,
        purge_all_soft_deleted,
        purge_excluded_messages,
        count_purge_candidates,
        prune_channel_progress,
        SOFT_DELETE_RETENTION_DAYS,
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

BACKUP_DB_PATH = os.getenv("BACKUP_DATA_PATH", "data/backup.db")
ATTACHMENTS_DIR = os.getenv("BACKUP_ATTACHMENTS_PATH", "data/backups/attachments")
PROGRESS_CHANNEL_NAME = "backup-restore-progress"
PURGE_INTERVAL_HOURS = float(os.getenv("BACKUP_PURGE_INTERVAL_HOURS", "24"))


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


class PurgeConfirmView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=90)
        self.confirmed = False

    @discord.ui.button(label="🗑️ Endgültig löschen", style=discord.ButtonStyle.danger)
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
            content="Purge abgebrochen.", embed=None, view=None
        )
        self.stop()


class BackupAdminCog(commands.Cog):
    """Admin-Hilfen + gezielt erweiterte Restore-Logik für Disaster-Recovery."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db_path = BACKUP_DB_PATH
        self._dl_task: Optional[asyncio.Task] = None
        self._purge_task: Optional[asyncio.Task] = None

    async def cog_load(self) -> None:
        await ensure_extra_tables(self.db_path)
        self.bot.loop.create_task(self._patch_backup_cog())
        self._purge_task = self.bot.loop.create_task(self._soft_delete_retention_loop())
        self.bot.loop.create_task(self._startup_prune_progress())

    async def cog_unload(self) -> None:
        if self._purge_task and not self._purge_task.done():
            self._purge_task.cancel()
            try:
                await self._purge_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _startup_prune_progress(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(12)
        try:
            await self._prune_progress_now()
        except Exception as e:
            print(f"[Backup] channel_progress startup prune: {e}")

    async def _prune_progress_now(self) -> dict[str, int]:
        live_channels: list[int] = []
        live_guilds: list[int] = []
        for g in self.bot.guilds:
            live_guilds.append(g.id)
            for c in g.channels:
                live_channels.append(c.id)
        return await prune_channel_progress(self.db_path, live_channels, live_guilds)

    async def _soft_delete_retention_loop(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(30)
        interval = max(1.0, PURGE_INTERVAL_HOURS) * 3600.0
        print(
            f"[Backup] Soft-delete retention: {SOFT_DELETE_RETENTION_DAYS} days, "
            f"check every {PURGE_INTERVAL_HOURS:g}h"
        )
        while True:
            try:
                stats = await purge_soft_deleted(
                    self.db_path,
                    ATTACHMENTS_DIR,
                    older_than_days=SOFT_DELETE_RETENTION_DAYS,
                )
                if stats["rows"] or stats.get("backfilled_deleted_at"):
                    print(
                        f"[Backup] Retention purge: {stats['rows']} rows, "
                        f"{stats['attachments']} attachment dirs, "
                        f"backfilled_deleted_at={stats.get('backfilled_deleted_at', 0)}"
                    )
                # Keep channel_progress in sync with live Discord + exclusions
                await self._prune_progress_now()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[Backup] Retention purge error: {e}")
                traceback.print_exc()
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """
                    UPDATE messages
                    SET is_deleted = 1, deleted_at = COALESCE(deleted_at, ?)
                    WHERE message_id = ?
                    """,
                    (int(time.time()), payload.message_id),
                )
                await db.commit()
        except Exception as e:
            print(f"[Backup] deleted_at update: {e}")

    async def _patch_backup_cog(self) -> None:
        await asyncio.sleep(2)
        cog = self.bot.get_cog("BackupCog")
        if cog is None:
            print("[BackupAdmin] BackupCog nicht gefunden – Patch übersprungen")
            return

        db_path = self.db_path
        admin = self

        async def load_snapshot_by_id(
            snapshot_id: int, guild_id: Optional[int] = None
        ) -> Optional[dict[str, Any]]:
            async with aiosqlite.connect(db_path) as db:
                async with db.execute(
                    "SELECT name, data, guild_id, created_at FROM snapshots WHERE id = ?",
                    (snapshot_id,),
                ) as cur:
                    row = await cur.fetchone()
            if not row:
                return None
            return {
                "name": row[0],
                "data": json.loads(row[1]),
                "source_guild_id": int(row[2]),
                "created_at": int(row[3]),
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

        original_restore = cog._restore_structure
        original_status = None
        for cmd in getattr(cog, "__cog_app_commands__", []):
            if cmd.name == "backup-status":
                original_status = cmd._callback
                break

        async def fixed_status(_cog: Any, interaction: discord.Interaction) -> None:
            # Prune stale/excluded channel_progress before showing counts
            try:
                await admin._prune_progress_now()
            except Exception as e:
                print(f"[Backup] status prune: {e}")
            if original_status is not None:
                await original_status(_cog, interaction)
            else:
                # Fallback: shouldn't happen
                await interaction.response.send_message("Status unavailable.", ephemeral=True)

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

                await original_restore(guild, data, current_progress, False)

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
                    for ch_data in list(data.get("channels", [])) + list(data.get("categories", [])):
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
                except Exception as e:
                    print(f"[Backup] channel_id_map: {e}")

                await bump("Alles erledigt.", done=True)
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

        async def fixed_snapshots(_cog: Any, interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            try:
                async with aiosqlite.connect(db_path) as db:
                    async with db.execute(
                        "SELECT id, name, created_at, guild_id, data FROM snapshots ORDER BY created_at DESC LIMIT 25"
                    ) as cur:
                        rows = await cur.fetchall()
            except Exception as e:
                await interaction.followup.send(f"❌ DB-Fehler: `{e}`", ephemeral=True)
                return
            if not rows:
                await interaction.followup.send("Noch keine Snapshots. Nutze `/backup-snapshot`.", ephemeral=True)
                return
            lines = []
            for snap_id, snap_name, created_at, src, data_json in rows:
                ts = datetime.fromtimestamp(created_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
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
                embed=discord.Embed(title="📦 Struktur-Snapshots", description="\n".join(lines), color=discord.Color.blurple()),
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
                await interaction.response.send_message("Nur auf einem Server nutzbar.", ephemeral=True)
                return
            if cog._msg_restore_task and not cog._msg_restore_task.done():
                await interaction.response.send_message("⚠️ Ein Nachrichten-Restore läuft bereits.", ephemeral=True)
                return
            snapshot_data: Optional[dict[str, Any]] = None
            as_of: Optional[int] = None
            try:
                if snapshot_id is not None:
                    snap = await load_snapshot_by_id(int(snapshot_id))
                    if snap:
                        snapshot_data = snap["data"]
                        as_of = snap.get("created_at")
                else:
                    snapshot_data = await load_latest_any()
                    as_of = None
                total = await count_messages(db_path, None, as_of=as_of, include_deleted=(as_of is None))
            except Exception as e:
                await interaction.response.send_message(f"❌ Fehler: `{e}`", ephemeral=True)
                return
            if total == 0:
                await interaction.response.send_message("Keine gespeicherten Nachrichten in der Backup-DB.", ephemeral=True)
                return
            scope = f"nur **#{channel.name}**" if channel else "**alle Channels**"
            limit_txt = f"max. **{limit}**/Channel" if limit else "**alle** Nachrichten"
            if as_of:
                ts = datetime.fromtimestamp(as_of, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                time_txt = f"Stand Snapshot **#{snapshot_id}** (`{ts}`) – auch später gelöschte"
            else:
                time_txt = "**Disaster-Modus**: alle Messages inkl. gelöschte (kein Snapshot-Zeitpunkt)"
            embed = discord.Embed(
                title="⚠️ Nachrichten-Restore bestätigen",
                description=(
                    f"Nachrichten: **{total:,}**\nZiel: {scope}\nLimit: {limit_txt}\n"
                    f"Name-Match: **{'an' if match_by_name else 'aus'}**\nZeitpunkt: {time_txt}\n\n"
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
                embed=discord.Embed(title="🔄 Nachrichten-Restore startet...", color=discord.Color.orange()),
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
                    as_of=as_of,
                    include_deleted=(as_of is None),
                )
            )

        cog._load_snapshot = load_snapshot_by_id  # type: ignore[method-assign]
        cog._load_latest_snapshot_data = load_latest_any  # type: ignore[method-assign]
        cog._build_overwrites = build_overwrites  # type: ignore[method-assign]
        cog._save_snapshot = save_snapshot_with_icon  # type: ignore[method-assign]
        cog._restore_structure = restore_with_extras  # type: ignore[method-assign]

        patched = ["restore_extras", "icon_snapshot", "as_of-deleted_at"]
        for cmd in getattr(cog, "__cog_app_commands__", []):
            if cmd.name == "backup-snapshots":
                cmd._callback = fixed_snapshots  # type: ignore[attr-defined]
                patched.append("backup-snapshots")
            elif cmd.name == "backup-restore-messages":
                cmd._callback = fixed_restore_messages  # type: ignore[attr-defined]
                patched.append("backup-restore-messages")
            elif cmd.name == "backup-status":
                cmd._callback = fixed_status  # type: ignore[attr-defined]
                patched.append("backup-status-prune")
        for cmd in self.bot.tree.get_commands():
            if cmd.name in ("backup-snapshots", "backup-restore-messages", "backup-status") and hasattr(
                cmd, "_callback"
            ):
                if cmd.name == "backup-snapshots":
                    cmd._callback = fixed_snapshots  # type: ignore[attr-defined]
                elif cmd.name == "backup-restore-messages":
                    cmd._callback = fixed_restore_messages  # type: ignore[attr-defined]
                elif cmd.name == "backup-status":
                    cmd._callback = fixed_status  # type: ignore[attr-defined]
                if cmd.name not in patched:
                    patched.append(cmd.name)
        print(f"[BackupAdmin] Patches OK: {patched}")

    # ---------- Purge / prune ----------

    @app_commands.command(
        name="backup-purge",
        description="Löscht soft-deleted und/oder excluded (Guilds+Channels) Nachrichten endgültig",
    )
    @app_commands.describe(
        soft_deleted="Alle is_deleted=1 Nachrichten hard-deleten (Default: an)",
        excluded="Nachrichten von excluded Guilds UND excluded Channels hard-deleten",
        confirm="Muss PURGE lauten zum Bestätigen",
    )
    @app_commands.default_permissions(administrator=True)
    async def backup_purge(
        self,
        interaction: discord.Interaction,
        soft_deleted: bool = True,
        excluded: bool = True,
        confirm: Optional[str] = None,
    ) -> None:
        if not soft_deleted and not excluded:
            await interaction.response.send_message(
                "Mindestens eine Option muss aktiv sein (`soft_deleted` und/oder `excluded`).",
                ephemeral=True,
            )
            return

        if (confirm or "").strip().upper() != "PURGE":
            stats = await count_purge_candidates(self.db_path)
            embed = discord.Embed(
                title="🗑️ Backup Purge – Vorschau",
                description=(
                    "**Hard-Delete** entfernt Zeilen aus der DB **und** Attachment-Ordner.\n"
                    "Das kann nicht rückgängig gemacht werden.\n\n"
                    "Zum Ausführen: `confirm` auf **PURGE** setzen.\n"
                    "Beispiel: `/backup-purge soft_deleted:True excluded:True confirm:PURGE`"
                ),
                color=discord.Color.orange(),
            )
            embed.add_field(
                name="Soft-deleted (is_deleted=1)",
                value=f"**{stats['soft_deleted']:,}**",
                inline=True,
            )
            embed.add_field(
                name=f"Davon >{stats['retention_days']} Tage alt",
                value=f"**{stats['soft_deleted_expired']:,}** (Auto-Purge)",
                inline=True,
            )
            embed.add_field(
                name="Excluded (Guilds)",
                value=f"**{stats['excluded_guild']:,}**",
                inline=True,
            )
            embed.add_field(
                name="Excluded (Channels)",
                value=f"**{stats.get('excluded_channel', 0):,}**",
                inline=True,
            )
            embed.add_field(
                name="DB total",
                value=f"**{stats['total']:,}**",
                inline=True,
            )
            embed.set_footer(
                text=f"Auto-Retention: {stats['retention_days']} Tage · alle {PURGE_INTERVAL_HOURS:g}h"
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        stats_preview = await count_purge_candidates(self.db_path)
        parts = []
        if soft_deleted:
            parts.append(f"• **{stats_preview['soft_deleted']:,}** soft-deleted Messages")
        if excluded:
            parts.append(
                f"• **{stats_preview.get('excluded', stats_preview['excluded_guild']):,}** "
                f"excluded Messages "
                f"(Guilds: {stats_preview['excluded_guild']:,} · "
                f"Channels: {stats_preview.get('excluded_channel', 0):,})"
            )

        embed = discord.Embed(
            title="⚠️ Purge bestätigen",
            description=(
                "Folgendes wird **endgültig** gelöscht:\n"
                + "\n".join(parts)
                + "\n\nAttachment-Ordner und `restored_messages`-Einträge werden mitgelöscht."
            ),
            color=discord.Color.red(),
        )
        view = PurgeConfirmView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        await view.wait()
        if not view.confirmed:
            return

        progress = await interaction.followup.send(
            embed=discord.Embed(title="🗑️ Purge läuft…", color=discord.Color.orange()),
            ephemeral=False,
        )

        total_rows = 0
        total_att = 0
        try:
            if soft_deleted:
                s = await purge_all_soft_deleted(self.db_path, ATTACHMENTS_DIR)
                total_rows += s["rows"]
                total_att += s["attachments"]
                print(f"[Backup] Manual purge soft-deleted: {s}")

            if excluded:
                s = await purge_excluded_messages(self.db_path, ATTACHMENTS_DIR)
                total_rows += s["rows"]
                total_att += s["attachments"]
                print(f"[Backup] Manual purge excluded: {s}")

            await progress.edit(
                embed=discord.Embed(
                    title="✅ Purge fertig",
                    description=(
                        f"Zeilen gelöscht: **{total_rows:,}**\n"
                        f"Attachment-Ordner: **{total_att:,}**"
                    ),
                    color=discord.Color.green(),
                )
            )
        except Exception as e:
            traceback.print_exc()
            await progress.edit(
                embed=discord.Embed(
                    title="❌ Purge fehlgeschlagen",
                    description=f"`{e}`",
                    color=discord.Color.red(),
                )
            )

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
        await add_excluded_guild(self.db_path, interaction.guild.id, reason or "Restore-Server")
        await interaction.response.send_message(
            f"✅ Server **{interaction.guild.name}** (`{interaction.guild.id}`) "
            f"wird nicht mehr geloggt/backfilled.\n"
            f"channel_progress für diesen Server wurde bereinigt.\n"
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
            if ok else f"ℹ️ **{interaction.guild.name}** war nicht excluded."
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
            await interaction.response.send_message("Keine excluded Guilds.", ephemeral=True)
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
            f"✅ **#{channel.name}** wird ab jetzt nicht mehr geloggt/backfilled.\n"
            f"channel_progress-Eintrag (falls vorhanden) wurde entfernt.",
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
            if ok else f"ℹ️ **#{channel.name}** war nicht excluded."
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
            embed=discord.Embed(title="⬇️ Missing Attachments…", description="Starte Download…", color=discord.Color.orange())
        )

        async def run() -> None:
            try:
                async def on_progress(processed: int, downloaded: int, failed: int) -> None:
                    try:
                        await progress.edit(
                            embed=discord.Embed(
                                title="⬇️ Missing Attachments…",
                                description=f"Geprüft: **{processed}**\nNeu: **{downloaded}** · Fehler: **{failed}**",
                                color=discord.Color.orange(),
                            )
                        )
                    except Exception:
                        pass

                stats = await download_missing_attachments(
                    self.db_path, ATTACHMENTS_DIR, guild_id=None, limit=int(limit), on_progress=on_progress
                )
                embed = discord.Embed(title="✅ Missing Attachments fertig", color=discord.Color.green())
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
                    embed=discord.Embed(title="❌ Download fehlgeschlagen", description=f"`{e}`", color=discord.Color.red())
                )
            finally:
                self._dl_task = None

        self._dl_task = asyncio.create_task(run())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BackupAdminCog(bot))
