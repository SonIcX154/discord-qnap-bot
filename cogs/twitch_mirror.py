from __future__ import annotations

import re
import asyncio
import logging
import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional, Any, Deque
from collections import OrderedDict, defaultdict, deque

log = logging.getLogger("qnapbot.twitch_mirror")

try:
    from twitchio.ext import commands as twitch_commands
except ImportError:
    twitch_commands = None  # type: ignore

try:
    import aiohttp
except ImportError:
    aiohttp = None  # type: ignore

try:
    from utils.twitch_map_store import TwitchMapStore
    from utils.twitch_helpers import (
        TWITCH_TOKEN,
        TWITCH_CHANNEL,
        TWITCH_DISCORD_CHANNEL_ID,
        TWITCH_NICK,
        TWITCH_CLIENT_ID,
        DISCORD_TO_TWITCH,
        SEND_DELAY,
        MSG_CACHE_MAX,
        TWITCH_MIRROR_DB_PATH,
        WEBHOOK_NAME,
        REQUIRED_DELETE_SCOPE,
        REQUIRED_SEND_SCOPE,
        CLEARMSG_RE,
        CLEARCHAT_USER_RE,
        LEADING_AT_RE,
        OWNER_PING_MAP,
        is_configured,
        normalize_token,
        bearer_token,
        safe_webhook_username,
        apply_owner_pings,
        allowed_mentions_for,
        twitch_msg_id,
        message_tags,
        normalize_content,
        strip_leading_reply_mention,
        reply_header_from_tags,
        discord_content_for_twitch,
        compose_mirror_content,
    )
except ImportError:
    from ..utils.twitch_map_store import TwitchMapStore
    from ..utils.twitch_helpers import (
        TWITCH_TOKEN,
        TWITCH_CHANNEL,
        TWITCH_DISCORD_CHANNEL_ID,
        TWITCH_NICK,
        TWITCH_CLIENT_ID,
        DISCORD_TO_TWITCH,
        SEND_DELAY,
        MSG_CACHE_MAX,
        TWITCH_MIRROR_DB_PATH,
        WEBHOOK_NAME,
        REQUIRED_DELETE_SCOPE,
        REQUIRED_SEND_SCOPE,
        CLEARMSG_RE,
        CLEARCHAT_USER_RE,
        LEADING_AT_RE,
        OWNER_PING_MAP,
        is_configured,
        normalize_token,
        bearer_token,
        safe_webhook_username,
        apply_owner_pings,
        allowed_mentions_for,
        twitch_msg_id,
        message_tags,
        normalize_content,
        strip_leading_reply_mention,
        reply_header_from_tags,
        discord_content_for_twitch,
        compose_mirror_content,
    )


class TwitchMirrorBot(twitch_commands.Bot if twitch_commands else object):  # type: ignore[misc]
    def __init__(
        self,
        discord_bot: commands.Bot,
        discord_channel_id: int,
        store: Optional[TwitchMapStore] = None,
    ) -> None:
        if twitch_commands is None:
            raise RuntimeError("twitchio is not installed")

        super().__init__(
            token=normalize_token(TWITCH_TOKEN),
            prefix="!",
            initial_channels=[TWITCH_CHANNEL],
        )
        self.discord_bot = discord_bot
        self.discord_channel_id = discord_channel_id
        self._queue: asyncio.Queue[
            tuple[str, str, str, Optional[str], Optional[str], bool]
        ] = asyncio.Queue()
        self._delete_queue: asyncio.Queue[tuple[str, Optional[str]]] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self.connected = False
        self._avatar_cache: dict[str, Optional[str]] = {}
        self._avatar_lock = asyncio.Lock()

        self._store = store or TwitchMapStore(TWITCH_MIRROR_DB_PATH, MSG_CACHE_MAX)

        self._msg_map: OrderedDict[str, int] = OrderedDict()
        self._login_msgs: dict[str, set[str]] = defaultdict(set)
        self._discord_to_inbound: OrderedDict[int, str] = OrderedDict()

        self._outbound_pending: Deque[int] = deque()
        self._discord_to_twitch: OrderedDict[int, str] = OrderedDict()
        self._outbound_twitch_ids: OrderedDict[str, None] = OrderedDict()
        self._bot_logins: set[str] = set()
        if TWITCH_NICK:
            self._bot_logins.add(TWITCH_NICK.lower().lstrip("#"))
        self._send_lock = asyncio.Lock()

        self._webhook: Optional[discord.Webhook] = None
        self._text_channel: Optional[discord.TextChannel] = None

        self._broadcaster_id: Optional[str] = None
        self._moderator_id: Optional[str] = None
        self._has_delete_scope: bool = False
        self._has_send_scope: bool = False
        self._helix_client_id: str = TWITCH_CLIENT_ID

    def _track_outbound_tid(self, twitch_id: str) -> None:
        self._outbound_twitch_ids[twitch_id] = None
        self._outbound_twitch_ids.move_to_end(twitch_id)
        while len(self._outbound_twitch_ids) > MSG_CACHE_MAX:
            self._outbound_twitch_ids.popitem(last=False)

    async def _load_persisted_map(self) -> None:
        try:
            await self._store.init()
            rows = await self._store.load_recent()
        except Exception as e:
            log.warning("Map DB load failed: %s", e)
            return

        inbound = 0
        outbound = 0
        for row in rows:
            tid = str(row["twitch_id"])
            did = int(row["discord_id"])
            login = (row.get("login") or "").lower()
            direction = row.get("direction") or "inbound"

            if direction == "outbound":
                self._discord_to_twitch[did] = tid
                self._discord_to_twitch.move_to_end(did)
                self._track_outbound_tid(tid)
                outbound += 1
            else:
                self._msg_map[tid] = did
                self._msg_map.move_to_end(tid)
                self._discord_to_inbound[did] = tid
                self._discord_to_inbound.move_to_end(did)
                if login:
                    self._login_msgs[login].add(tid)
                inbound += 1

        if inbound or outbound:
            log.info(
                "Restored message map from DB: inbound=%s outbound=%s path=%s",
                inbound, outbound, self._store.path,
            )

    async def _remember(self, twitch_id: str, discord_id: int, login: str) -> None:
        self._msg_map[twitch_id] = discord_id
        self._msg_map.move_to_end(twitch_id)
        self._login_msgs[login.lower()].add(twitch_id)
        self._discord_to_inbound[discord_id] = twitch_id
        self._discord_to_inbound.move_to_end(discord_id)
        while len(self._msg_map) > MSG_CACHE_MAX:
            old_tid, old_did = self._msg_map.popitem(last=False)
            for s in self._login_msgs.values():
                s.discard(old_tid)
            self._discord_to_inbound.pop(old_did, None)
        while len(self._discord_to_inbound) > MSG_CACHE_MAX:
            self._discord_to_inbound.popitem(last=False)
        try:
            await self._store.upsert(
                twitch_id=twitch_id,
                discord_id=discord_id,
                login=login,
                direction="inbound",
            )
        except Exception as e:
            log.warning("Map persist (inbound) failed: %s", e)

    async def _forget_twitch_id(self, twitch_id: str) -> Optional[int]:
        """Resolve + drop mapping for a Twitch msg id (inbound or outbound)."""
        discord_id = self._msg_map.pop(twitch_id, None)
        for s in self._login_msgs.values():
            s.discard(twitch_id)
        if discord_id is not None:
            self._discord_to_inbound.pop(discord_id, None)

        # Outbound memory (Discord original → Twitch)
        if discord_id is None:
            for did, tid in list(self._discord_to_twitch.items()):
                if tid == twitch_id:
                    discord_id = did
                    self._discord_to_twitch.pop(did, None)
                    break
        self._outbound_twitch_ids.pop(twitch_id, None)

        try:
            row = await self._store.delete_by_twitch(twitch_id)
            if row is not None:
                if discord_id is None:
                    discord_id = int(row["discord_id"])
                # Keep memory consistent with DB direction
                if row.get("direction") == "outbound" and discord_id is not None:
                    self._discord_to_twitch.pop(discord_id, None)
                elif row.get("direction") == "inbound" and discord_id is not None:
                    self._discord_to_inbound.pop(discord_id, None)
        except Exception as e:
            log.warning("Map DB delete (twitch) failed: %s", e)
        return discord_id

    async def _forget_inbound_by_discord(self, discord_id: int) -> Optional[str]:
        twitch_id = self._discord_to_inbound.pop(discord_id, None)
        if twitch_id is not None:
            self._msg_map.pop(twitch_id, None)
            for s in self._login_msgs.values():
                s.discard(twitch_id)
        try:
            row = await self._store.delete_by_discord(discord_id, direction="inbound")
            if twitch_id is None and row is not None:
                twitch_id = str(row["twitch_id"])
        except Exception as e:
            log.warning("Map DB delete (discord inbound) failed: %s", e)
        return twitch_id

    async def _remember_outbound(self, discord_id: int, twitch_id: str) -> None:
        self._discord_to_twitch[discord_id] = twitch_id
        self._discord_to_twitch.move_to_end(discord_id)
        self._track_outbound_tid(twitch_id)
        while len(self._discord_to_twitch) > MSG_CACHE_MAX:
            self._discord_to_twitch.popitem(last=False)
        try:
            await self._store.upsert(
                twitch_id=twitch_id,
                discord_id=discord_id,
                login=None,
                direction="outbound",
            )
        except Exception as e:
            log.warning("Map persist (outbound) failed: %s", e)

    async def _forget_outbound(self, discord_id: int) -> Optional[str]:
        twitch_id = self._discord_to_twitch.pop(discord_id, None)
        if twitch_id is not None:
            self._outbound_twitch_ids.pop(twitch_id, None)
        try:
            row = await self._store.delete_by_discord(discord_id, direction="outbound")
            if twitch_id is None and row is not None:
                twitch_id = str(row["twitch_id"])
                self._outbound_twitch_ids.pop(twitch_id, None)
        except Exception as e:
            log.warning("Map DB delete (outbound) failed: %s", e)
        return twitch_id

    def _helix_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {bearer_token(TWITCH_TOKEN)}",
            "Client-Id": self._helix_client_id or TWITCH_CLIENT_ID,
            "Content-Type": "application/json",
        }

    async def _resolve_helix_ids(self) -> None:
        if aiohttp is None:
            log.warning("Helix unavailable (aiohttp missing)")
            return

        bearer = bearer_token(TWITCH_TOKEN)
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    "https://id.twitch.tv/oauth2/validate",
                    headers={"Authorization": f"OAuth {bearer}"},
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.warning(
                            "Token validate HTTP %s: %s",
                            resp.status, body[:200],
                        )
                        return
                    info: dict[str, Any] = await resp.json()

                self._moderator_id = str(info.get("user_id") or "") or None
                scopes = list(info.get("scopes") or [])
                self._has_delete_scope = REQUIRED_DELETE_SCOPE in scopes
                self._has_send_scope = REQUIRED_SEND_SCOPE in scopes
                login = info.get("login") or "?"
                if login and login != "?":
                    self._bot_logins.add(str(login).lower())
                token_client_id = str(info.get("client_id") or "")

                if token_client_id:
                    if TWITCH_CLIENT_ID and TWITCH_CLIENT_ID != token_client_id:
                        log.warning(
                            "TWITCH_CLIENT_ID mismatch!\n"
                            "  .env TWITCH_CLIENT_ID = %s\n"
                            "  token client_id       = %s\n"
                            "  → using token's client_id for Helix calls",
                            TWITCH_CLIENT_ID, token_client_id,
                        )
                    self._helix_client_id = token_client_id
                elif TWITCH_CLIENT_ID:
                    self._helix_client_id = TWITCH_CLIENT_ID
                else:
                    log.warning("No Client-Id available")
                    return

                log.info(
                    "Token user=%s id=%s client_id=%s… scopes=%s",
                    login, self._moderator_id, self._helix_client_id[:8], len(scopes),
                )
                if self._has_delete_scope:
                    log.info("Scope OK: %s", REQUIRED_DELETE_SCOPE)
                else:
                    log.warning("missing `%s`", REQUIRED_DELETE_SCOPE)
                if self._has_send_scope:
                    log.info("Scope OK: %s", REQUIRED_SEND_SCOPE)
                else:
                    log.warning(
                        "missing `%s` — Discord→Twitch will use IRC (no reliable msg id)",
                        REQUIRED_SEND_SCOPE,
                    )

                headers = self._helix_headers()
                async with session.get(
                    f"https://api.twitch.tv/helix/users?login={TWITCH_CHANNEL}",
                    headers=headers,
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.warning(
                            "Helix users (broadcaster) HTTP %s: %s",
                            resp.status, body[:200],
                        )
                        return
                    data = await resp.json()
                users = data.get("data") or []
                if not users:
                    log.warning("Broadcaster login not found: %s", TWITCH_CHANNEL)
                    return
                self._broadcaster_id = str(users[0]["id"])
                log.info(
                    "Helix ready: broadcaster=%s sender/mod=%s",
                    self._broadcaster_id, self._moderator_id,
                )
        except Exception as e:
            log.exception("Helix id resolve failed: %s", e)

    async def event_ready(self) -> None:
        self.connected = True
        nick = getattr(self, "nick", None) or TWITCH_NICK or "?"
        if nick and nick != "?":
            self._bot_logins.add(str(nick).lower().lstrip("#"))
        log.info("Connected as %s → #%s", nick, TWITCH_CHANNEL)
        if OWNER_PING_MAP:
            log.info(
                "Owner ping map (@ or bare): %s",
                ", ".join(f"{k}→<@{v}>" for k, v in OWNER_PING_MAP.items()),
            )
        log.info("Echo filter: only [Discord]-prefixed (bot self-chat mirrored)")
        await self._load_persisted_map()
        await self._resolve_helix_ids()
        if DISCORD_TO_TWITCH:
            log.info("Discord → Twitch: ON (Helix preferred)")
        else:
            log.info("Discord → Twitch: OFF")
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._discord_worker())

    async def event_message(self, message) -> None:  # type: ignore[no-untyped-def]
        try:
            if getattr(message, "echo", False):
                tid = twitch_msg_id(message)
                if tid and self._outbound_pending:
                    discord_id = self._outbound_pending.popleft()
                    await self._remember_outbound(discord_id, tid)
                    log.debug(
                        "Outbound (IRC echo) linked discord=%s → twitch=%s…",
                        discord_id, tid[:12],
                    )
                return

            author = message.author
            login = (getattr(author, "name", None) or "unknown").lower()
            display = (
                getattr(author, "display_name", None)
                or getattr(author, "name", None)
                or "unknown"
            )
            raw = message.content or ""
            content, is_action = normalize_content(raw)
            if not content:
                return

            tid = twitch_msg_id(message)

            if tid and tid in self._outbound_twitch_ids:
                return

            if content.startswith("[Discord]") or content.lower().startswith("[discord]"):
                if tid and self._outbound_pending and login in self._bot_logins:
                    discord_id = self._outbound_pending.popleft()
                    await self._remember_outbound(discord_id, tid)
                    log.debug(
                        "Outbound ([Discord] IRC) linked discord=%s → twitch=%s…",
                        discord_id, tid[:12],
                    )
                return

            tags = message_tags(message)
            reply_header: Optional[str] = None
            try:
                reply_header = reply_header_from_tags(tags)
                if reply_header:
                    before = content
                    content = strip_leading_reply_mention(content, tags)
                    if content != before:
                        log.debug(
                            "stripped reply @mention: %r → %r",
                            before[:40], content[:40],
                        )
            except Exception as e:
                log.warning("reply format error (sending plain): %s", e)
                reply_header = None

            if not content:
                return

            if not tid:
                log.warning("no msg id from IRC for @%s", login)

            await self._queue.put(
                (login, display, content[:1900], tid, reply_header, is_action)
            )
        except Exception as e:
            log.exception("event_message error: %s", e)

    async def event_raw_data(self, data: str) -> None:  # type: ignore[no-untyped-def]
        if not data:
            return

        if "CLEARMSG" in data:
            m = CLEARMSG_RE.search(data)
            if m:
                tid = m.group(1).strip()
                if tid:
                    await self._delete_queue.put(("id", tid))
            return

        if "CLEARCHAT" in data:
            m = CLEARCHAT_USER_RE.search(data)
            if m:
                login = m.group(1).strip().lower()
                await self._delete_queue.put(("user", login))
                return
            if re.search(r"CLEARCHAT\s+#\S+\s*$", data.strip(), re.IGNORECASE):
                await self._delete_queue.put(("all", None))

    async def event_message_delete(self, message) -> None:  # type: ignore[no-untyped-def]
        tid = twitch_msg_id(message)
        if tid:
            await self._delete_queue.put(("id", tid))

    async def send_to_twitch(self, text: str, discord_msg_id: int) -> bool:
        text = (text or "").strip()
        if not text:
            return False

        async with self._send_lock:
            if (
                aiohttp is not None
                and self._helix_client_id
                and self._broadcaster_id
                and self._moderator_id
                and self._has_send_scope
            ):
                try:
                    url = "https://api.twitch.tv/helix/chat/messages"
                    payload = {
                        "broadcaster_id": self._broadcaster_id,
                        "sender_id": self._moderator_id,
                        "message": text[:500],
                    }
                    timeout = aiohttp.ClientTimeout(total=10)
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.post(
                            url, json=payload, headers=self._helix_headers()
                        ) as resp:
                            body_txt = await resp.text()
                            if resp.status in (200, 201):
                                try:
                                    data = await resp.json(content_type=None)
                                except Exception:
                                    import json as _json

                                    data = _json.loads(body_txt)
                                rows = data.get("data") or []
                                mid = None
                                if rows:
                                    mid = rows[0].get("message_id")
                                    is_sent = rows[0].get("is_sent", True)
                                    if not is_sent:
                                        drop = rows[0].get("drop_reason")
                                        log.warning("Helix send dropped: %s", drop)
                                        return False
                                if mid:
                                    await self._remember_outbound(discord_msg_id, str(mid))
                                    log.debug(
                                        "Helix send OK discord=%s → twitch=%s…",
                                        discord_msg_id, str(mid)[:12],
                                    )
                                else:
                                    log.warning(
                                        "Helix send OK but no message_id in response: %s",
                                        body_txt[:200],
                                    )
                                await asyncio.sleep(SEND_DELAY)
                                return True
                            log.warning(
                                "Helix send HTTP %s: %s",
                                resp.status, body_txt[:250],
                            )
                except Exception as e:
                    log.error("Helix send error: %s", e)

            if not self.connected:
                log.warning("send_to_twitch: not connected")
                return False
            try:
                channel = self.get_channel(TWITCH_CHANNEL)
                if channel is None:
                    channel = self.get_channel(f"#{TWITCH_CHANNEL}")
                if channel is None:
                    log.warning("No IRC channel for #%s", TWITCH_CHANNEL)
                    return False

                self._outbound_pending.append(discord_msg_id)
                await channel.send(text[:480])
                await asyncio.sleep(SEND_DELAY)
                log.debug(
                    "IRC send used (no Helix msg id — delete-from-Discord may not work for this message)"
                )
                return True
            except Exception as e:
                try:
                    if self._outbound_pending and self._outbound_pending[-1] == discord_msg_id:
                        self._outbound_pending.pop()
                except Exception:
                    pass
                log.error("IRC send failed: %s", e)
                return False

    async def delete_on_twitch(self, twitch_msg_id: str) -> bool:
        if not twitch_msg_id:
            return False
        if aiohttp is None:
            log.warning("delete failed: aiohttp missing")
            return False
        if not self._helix_client_id:
            log.warning("delete failed: no Client-Id")
            return False
        if not self._broadcaster_id or not self._moderator_id:
            log.warning("delete failed: broadcaster/moderator id unknown")
            return False
        if not self._has_delete_scope:
            log.warning(
                "delete failed: missing scope `%s`",
                REQUIRED_DELETE_SCOPE,
            )
            return False

        try:
            url = "https://api.twitch.tv/helix/moderation/chat"
            params = {
                "broadcaster_id": self._broadcaster_id,
                "moderator_id": self._moderator_id,
                "message_id": twitch_msg_id,
            }
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.delete(
                    url, params=params, headers=self._helix_headers()
                ) as resp:
                    if resp.status in (200, 204):
                        log.debug(
                            "Helix delete OK %s… HTTP %s",
                            twitch_msg_id[:12], resp.status,
                        )
                        return True
                    body = await resp.text()
                    log.warning(
                        "Helix delete FAILED HTTP %s\n"
                        "  msg_id=%s\n"
                        "  broadcaster=%s moderator=%s\n"
                        "  body=%s",
                        resp.status, twitch_msg_id,
                        self._broadcaster_id, self._moderator_id, body[:300],
                    )
                    return False
        except Exception as e:
            log.error("Helix delete error: %s", e)
            return False

    async def _fetch_avatar(self, login: str) -> Optional[str]:
        login = login.lower()
        if login in self._avatar_cache:
            return self._avatar_cache[login]

        if not self._helix_client_id or aiohttp is None:
            self._avatar_cache[login] = None
            return None

        async with self._avatar_lock:
            if login in self._avatar_cache:
                return self._avatar_cache[login]

            url = f"https://api.twitch.tv/helix/users?login={login}"
            try:
                timeout = aiohttp.ClientTimeout(total=8)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url, headers=self._helix_headers()) as resp:
                        if resp.status != 200:
                            log.debug("Helix users %s: HTTP %s", login, resp.status)
                            self._avatar_cache[login] = None
                            return None
                        data: dict[str, Any] = await resp.json()
                users = data.get("data") or []
                if not users:
                    self._avatar_cache[login] = None
                    return None
                avatar = users[0].get("profile_image_url") or None
                self._avatar_cache[login] = avatar
                return avatar
            except Exception as e:
                log.warning("Avatar fetch failed for %s: %s", login, e)
                self._avatar_cache[login] = None
                return None

    async def _get_or_create_webhook(
        self, channel: discord.TextChannel
    ) -> Optional[discord.Webhook]:
        me = channel.guild.me if channel.guild else None
        if me and not channel.permissions_for(me).manage_webhooks:
            log.warning("Missing Manage Webhooks permission")
            return None

        try:
            hooks = await channel.webhooks()
            for h in hooks:
                if h.name == WEBHOOK_NAME and h.token:
                    log.debug("Reusing webhook id=%s", h.id)
                    return h

            hook = await channel.create_webhook(
                name=WEBHOOK_NAME,
                reason="Twitch chat mirror",
            )
            log.info("Created webhook id=%s", hook.id)
            return hook
        except Exception as e:
            log.error("Webhook setup failed: %s", e)
            return None

    async def _delete_discord_message(self, discord_msg_id: int) -> None:
        """Delete a mirrored Discord message (webhook OR normal user message)."""
        self._discord_to_inbound.pop(discord_msg_id, None)

        deleted = False
        webhook = self._webhook
        if webhook is not None and webhook.token:
            try:
                await webhook.delete_message(discord_msg_id)
                deleted = True
            except discord.NotFound:
                # Not a webhook message (e.g. Discord→Twitch original) — try channel
                pass
            except Exception as e:
                log.warning("Webhook delete failed: %s", e)

        if deleted:
            return

        channel = self._text_channel
        if channel is not None:
            try:
                msg = await channel.fetch_message(discord_msg_id)
                await msg.delete()
            except discord.NotFound:
                pass
            except Exception as e:
                log.warning("Channel delete failed: %s", e)

    async def _process_delete(self, kind: str, value: Optional[str]) -> None:
        if kind == "id" and value:
            discord_id = await self._forget_twitch_id(value)
            if discord_id is not None:
                await self._delete_discord_message(discord_id)
                log.debug(
                    "Deleted Discord msg=%s (CLEARMSG %s…)",
                    discord_id, value[:8],
                )
            return

        if kind == "user" and value:
            login = value.lower()
            tids = set(self._login_msgs.get(login, set()))
            try:
                db_rows = await self._store.delete_by_login(login)
                for row in db_rows:
                    tids.add(str(row["twitch_id"]))
            except Exception as e:
                log.warning("Map DB delete_by_login failed: %s", e)

            deleted = 0
            for tid in list(tids):
                discord_id = await self._forget_twitch_id(tid)
                if discord_id is not None:
                    await self._delete_discord_message(discord_id)
                    deleted += 1
                    await asyncio.sleep(0.25)
            if deleted:
                log.info("CLEARCHAT @%s: removed %s Discord msg(s)", login, deleted)
            return

        if kind == "all":
            items = list(self._msg_map.items())
            self._msg_map.clear()
            self._login_msgs.clear()
            self._discord_to_inbound.clear()
            # Also wipe outbound so Discord originals of Discord→Twitch go away
            outbound_items = list(self._discord_to_twitch.items())
            self._discord_to_twitch.clear()
            self._outbound_twitch_ids.clear()
            try:
                db_rows = await self._store.clear_direction("inbound")
                seen = {tid for tid, _ in items}
                for row in db_rows:
                    tid = str(row["twitch_id"])
                    if tid not in seen:
                        items.append((tid, int(row["discord_id"])))
                out_rows = await self._store.clear_direction("outbound")
                seen_out = {did for did, _ in outbound_items}
                for row in out_rows:
                    did = int(row["discord_id"])
                    if did not in seen_out:
                        outbound_items.append((did, str(row["twitch_id"])))
            except Exception as e:
                log.warning("Map DB clear failed: %s", e)

            deleted = 0
            for _, discord_id in items:
                await self._delete_discord_message(discord_id)
                deleted += 1
                await asyncio.sleep(0.25)
            for discord_id, _ in outbound_items:
                await self._delete_discord_message(discord_id)
                deleted += 1
                await asyncio.sleep(0.25)
            if deleted:
                log.info("Full CLEARCHAT: removed %s Discord msg(s)", deleted)

    async def _discord_worker(self) -> None:
        await self.discord_bot.wait_until_ready()
        channel = self.discord_bot.get_channel(self.discord_channel_id)
        if channel is None:
            try:
                channel = await self.discord_bot.fetch_channel(self.discord_channel_id)
            except Exception as e:
                log.error(
                    "Cannot resolve Discord channel %s: %s",
                    self.discord_channel_id, e,
                )
                return

        if not isinstance(channel, discord.TextChannel):
            log.error("Channel %s is not a text channel", self.discord_channel_id)
            return

        self._text_channel = channel
        webhook = await self._get_or_create_webhook(channel)
        self._webhook = webhook
        if webhook is None:
            log.warning("Falling back to bot messages (no webhook)")

        log.info(
            "Mirroring #%s → #%s (webhook=%s)",
            TWITCH_CHANNEL, channel.name, "yes" if webhook else "no",
        )

        while True:
            try:
                try:
                    kind, value = self._delete_queue.get_nowait()
                    await self._process_delete(kind, value)
                    continue
                except asyncio.QueueEmpty:
                    pass

                try:
                    item = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    get_msg = asyncio.create_task(self._queue.get())
                    get_del = asyncio.create_task(self._delete_queue.get())
                    done, pending = await asyncio.wait(
                        {get_msg, get_del}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for t in pending:
                        t.cancel()
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass
                    task = next(iter(done))
                    result = task.result()
                    if task is get_del:
                        kind, value = result
                        await self._process_delete(kind, value)
                        continue
                    item = result

                login, display, content, twitch_id, reply_header, is_action = item
                username = safe_webhook_username(display)
                avatar_url = await self._fetch_avatar(login)

                if reply_header and content.startswith("@"):
                    stripped = LEADING_AT_RE.sub("", content, count=1).strip()
                    if stripped:
                        content = stripped

                content, ping_ids = apply_owner_pings(content)
                final = compose_mirror_content(content, reply_header, is_action)
                if not final.strip():
                    continue
                mentions = allowed_mentions_for(ping_ids)

                discord_msg_id: Optional[int] = None

                if webhook is not None:
                    try:
                        sent = await webhook.send(
                            content=final,
                            username=username,
                            avatar_url=avatar_url or discord.utils.MISSING,
                            wait=True,
                            allowed_mentions=mentions,
                        )
                        discord_msg_id = sent.id
                    except discord.NotFound:
                        log.warning("Webhook missing – recreating")
                        webhook = await self._get_or_create_webhook(channel)
                        self._webhook = webhook
                        if webhook is not None:
                            sent = await webhook.send(
                                content=final,
                                username=username,
                                avatar_url=avatar_url or discord.utils.MISSING,
                                wait=True,
                                allowed_mentions=mentions,
                            )
                            discord_msg_id = sent.id
                    except discord.HTTPException as e:
                        log.warning("Webhook send failed: %s", e)
                        if reply_header:
                            try:
                                sent = await webhook.send(
                                    content=content,
                                    username=username,
                                    avatar_url=avatar_url or discord.utils.MISSING,
                                    wait=True,
                                    allowed_mentions=mentions,
                                )
                                discord_msg_id = sent.id
                            except Exception as e2:
                                log.warning("Plain retry failed: %s", e2)
                        else:
                            try:
                                sent = await channel.send(
                                    f"**{username}**: {final}",
                                    allowed_mentions=mentions,
                                )
                                discord_msg_id = sent.id
                            except Exception as e2:
                                log.warning("Fallback send failed: %s", e2)
                else:
                    sent = await channel.send(
                        f"**{username}**: {final}",
                        allowed_mentions=mentions,
                    )
                    discord_msg_id = sent.id

                if twitch_id and discord_msg_id is not None:
                    await self._remember(twitch_id, discord_msg_id, login)

                await asyncio.sleep(SEND_DELAY)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("Worker error: %s", e)
                await asyncio.sleep(1.0)


class TwitchMirrorCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._twitch: Optional[TwitchMirrorBot] = None
        self._task: Optional[asyncio.Task] = None
        self._store = TwitchMapStore(TWITCH_MIRROR_DB_PATH, MSG_CACHE_MAX)
        try:
            self._discord_channel_id = (
                int(TWITCH_DISCORD_CHANNEL_ID) if TWITCH_DISCORD_CHANNEL_ID else 0
            )
        except ValueError:
            self._discord_channel_id = 0

    async def cog_load(self) -> None:
        if twitch_commands is None:
            log.warning("twitchio not installed – cog idle")
            return
        if not is_configured():
            log.warning(
                "Disabled – set TWITCH_TOKEN, TWITCH_CHANNEL, "
                "TWITCH_DISCORD_CHANNEL_ID in .env"
            )
            return

        try:
            channel_id = int(TWITCH_DISCORD_CHANNEL_ID)
        except ValueError:
            log.error(
                "Invalid TWITCH_DISCORD_CHANNEL_ID: %r",
                TWITCH_DISCORD_CHANNEL_ID,
            )
            return

        try:
            await self._store.init()
            log.info("Map DB: %s", self._store.path)
        except Exception as e:
            log.error("Map DB init failed: %s", e)

        self._discord_channel_id = channel_id
        self._twitch = TwitchMirrorBot(self.bot, channel_id, store=self._store)
        self._task = asyncio.create_task(self._run_twitch())
        log.info("Starting client for #%s…", TWITCH_CHANNEL)

    async def _run_twitch(self) -> None:
        assert self._twitch is not None
        backoff = 5.0
        while True:
            try:
                await self._twitch.start()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Connection error: %s – retry in %.0fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 120.0)
            else:
                log.info("Disconnected – reconnecting in 10s")
                await asyncio.sleep(10.0)
                backoff = 5.0

    async def cog_unload(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._twitch is not None:
            try:
                await self._twitch.close()
            except Exception:
                pass
            if self._twitch._worker_task and not self._twitch._worker_task.done():
                self._twitch._worker_task.cancel()

    def _is_mirror_channel(self, channel_id: Optional[int]) -> bool:
        return bool(self._discord_channel_id and channel_id == self._discord_channel_id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not DISCORD_TO_TWITCH:
            return
        if not self._is_mirror_channel(message.channel.id if message.channel else None):
            return
        if self._twitch is None or not self._twitch.connected:
            return

        if message.author.bot:
            return
        if message.webhook_id is not None:
            return
        if message.type not in (
            discord.MessageType.default,
            discord.MessageType.reply,
        ):
            return

        body = discord_content_for_twitch(message)
        if not body:
            return

        display = (message.author.display_name or message.author.name or "Discord").strip()
        display = display.replace("\n", " ")[:32]
        payload = f"[Discord] {display}: {body}"
        if len(payload) > 480:
            payload = payload[:477] + "…"

        ok = await self._twitch.send_to_twitch(payload, message.id)
        if ok:
            log.debug("Discord→Twitch: %s: %r", display, body[:60])

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if not self._is_mirror_channel(payload.channel_id):
            return
        if self._twitch is None or not self._twitch.connected:
            return

        twitch_id = await self._twitch._forget_inbound_by_discord(payload.message_id)
        source = "inbound (Twitch original)"

        if not twitch_id and DISCORD_TO_TWITCH:
            twitch_id = await self._twitch._forget_outbound(payload.message_id)
            source = "outbound (Discord→Twitch)"

        if not twitch_id:
            log.debug(
                "Discord delete msg=%s not in map (inbound=%s outbound=%s) — skip Twitch delete",
                payload.message_id,
                len(self._twitch._discord_to_inbound),
                len(self._twitch._discord_to_twitch),
            )
            return

        ok = await self._twitch.delete_on_twitch(twitch_id)
        if ok:
            log.debug(
                "Discord delete → Twitch delete %s… (%s)",
                twitch_id[:12], source,
            )
        else:
            log.warning(
                "Could not delete on Twitch (%s…, %s). See Helix error above.",
                twitch_id[:12], source,
            )

    @app_commands.command(
        name="twitch-mirror-status",
        description="Shows Twitch chat mirror status",
    )
    @app_commands.default_permissions(administrator=True)
    async def twitch_mirror_status(self, interaction: discord.Interaction) -> None:
        if not is_configured():
            await interaction.response.send_message(
                "Twitch mirror is **not configured**.",
                ephemeral=True,
            )
            return

        connected = bool(self._twitch and self._twitch.connected)
        tracked = len(self._twitch._msg_map) if self._twitch else 0
        outbound = len(self._twitch._discord_to_twitch) if self._twitch else 0
        has_del = bool(self._twitch and self._twitch._has_delete_scope)
        has_send = bool(self._twitch and self._twitch._has_send_scope)
        helix_ready = bool(
            self._twitch
            and self._twitch._broadcaster_id
            and self._twitch._moderator_id
            and self._twitch._helix_client_id
        )

        db_counts = {"total": 0, "inbound": 0, "outbound": 0}
        try:
            db_counts = await self._store.count()
        except Exception:
            pass

        embed = discord.Embed(
            title="Twitch Mirror Status",
            color=discord.Color.green() if connected else discord.Color.orange(),
        )
        embed.add_field(name="Twitch channel", value=f"`#{TWITCH_CHANNEL}`", inline=True)
        embed.add_field(
            name="Discord channel",
            value=f"<#{TWITCH_DISCORD_CHANNEL_ID}>",
            inline=True,
        )
        embed.add_field(
            name="Connection",
            value="🟢 connected" if connected else "🔴 disconnected / starting",
            inline=True,
        )
        embed.add_field(
            name="In memory",
            value=f"inbound `{tracked}` · outbound `{outbound}`",
            inline=True,
        )
        embed.add_field(
            name="Persisted (DB)",
            value=(
                f"total `{db_counts['total']}` · "
                f"in `{db_counts['inbound']}` · out `{db_counts['outbound']}`"
            ),
            inline=True,
        )
        embed.add_field(
            name="Discord → Twitch",
            value=(
                f"ON · send={'Helix' if has_send else 'IRC'}"
                if DISCORD_TO_TWITCH
                else "OFF"
            ),
            inline=True,
        )
        embed.add_field(
            name="Helix",
            value=(
                f"ids={'yes' if helix_ready else 'no'} · "
                f"delete={'yes' if has_del else 'NO'} · "
                f"send={'yes' if has_send else 'NO'}"
            ),
            inline=False,
        )
        embed.set_footer(text=f"Map DB: {self._store.path} · max {MSG_CACHE_MAX}")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TwitchMirrorCog(bot))
