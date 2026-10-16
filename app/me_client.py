"""Telethon client logged in as the owner ("Send as me" + finding forwarded messages).

Optional: only used when TELETHON_ENABLED=true and the one-time login was done.
"""
from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError, RPCError

from .config import TelethonConfig

log = logging.getLogger(__name__)


@dataclass
class Located:
    peer_id: int          # Telethon "marked" peer id
    msg_id: int
    chat_title: str
    sender_id: int | None
    sender_name: str
    text: str
    ts: int


def _norm(text: str | None) -> str:
    return " ".join(unicodedata.normalize("NFC", text or "").split())


def _display_name(entity) -> str:
    if entity is None:
        return "Unknown"
    name = utils.get_display_name(entity)
    return name or "Unknown"


class MeClient:
    def __init__(self, cfg: TelethonConfig):
        self.cfg = cfg
        self.client: TelegramClient | None = None
        self.ready = False
        self.my_id: int | None = None

    async def start(self) -> None:
        if not self.cfg.enabled:
            return
        self.client = TelegramClient(self.cfg.session_path, self.cfg.api_id, self.cfg.api_hash)
        await self.client.connect()
        if not await self.client.is_user_authorized():
            log.error(
                "Telethon session is not logged in. Run the one-time login "
                "(see README: 'docker compose run --rm bot python -m app.login'). "
                "'Send as me' is disabled until then."
            )
            return
        me = await self.client.get_me()
        self.my_id = me.id
        await self._refresh_dialogs()
        self.ready = True
        log.info("Telethon ready as %s", _display_name(me))

    async def stop(self) -> None:
        if self.client:
            await self.client.disconnect()

    async def _refresh_dialogs(self) -> None:
        assert self.client
        # Fills Telethon's entity cache so chat ids can be resolved later.
        await self.client.get_dialogs(limit=300)

    async def _entity(self, peer_id: int):
        assert self.client
        try:
            return await self.client.get_input_entity(peer_id)
        except ValueError:
            await self._refresh_dialogs()
            return await self.client.get_input_entity(peer_id)

    # ---- finding the original of a forwarded message -------------------

    async def locate_forward(self, text: str, ts: int, sender_id: int | None) -> Located | None:
        if not self.ready or not self.client or not _norm(text):
            return None
        want = _norm(text)
        query = want[:60]

        async def matches(msg) -> bool:
            return (
                msg is not None
                and abs(int(msg.date.timestamp()) - ts) <= 1
                and _norm(msg.message) == want
            )

        try:
            # 1) Direct look in the sender's private chat when the id is known.
            if sender_id:
                try:
                    entity = await self._entity(sender_id)
                    offset = datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(seconds=2)
                    async for msg in self.client.iter_messages(entity, offset_date=offset, limit=30):
                        if await matches(msg):
                            return await self._to_located(msg)
                except (ValueError, RPCError):
                    pass
            # 2) Global search across all chats (works even when the sender is hidden).
            async for msg in self.client.iter_messages(None, search=query, limit=50):
                if await matches(msg):
                    return await self._to_located(msg)
        except FloodWaitError as exc:
            log.warning("Telegram asked to slow down for %ss while searching", exc.seconds)
        except RPCError as exc:
            log.warning("Search for forwarded message failed: %s", exc)
        return None

    async def _to_located(self, msg) -> Located:
        chat = await msg.get_chat()
        sender = await msg.get_sender()
        return Located(
            peer_id=utils.get_peer_id(msg.peer_id),
            msg_id=msg.id,
            chat_title=_display_name(chat),
            sender_id=msg.sender_id,
            sender_name=_display_name(sender),
            text=msg.message or "",
            ts=int(msg.date.timestamp()),
        )

    async def context_lines(self, peer_id: int, msg_id: int, limit: int) -> list[str]:
        """Last `limit` text messages up to and including msg_id, oldest first."""
        if not self.ready or not self.client:
            return []
        entity = await self._entity(peer_id)
        lines: list[str] = []
        async for msg in self.client.iter_messages(entity, max_id=msg_id + 1, limit=limit):
            if not msg.message:
                continue
            if msg.out:
                who = "Me"
            else:
                who = _display_name(await msg.get_sender())
            lines.append(f"{who}: {msg.message}")
        lines.reverse()
        return lines

    # ---- sending as the owner ------------------------------------------

    async def resolve_group_reply(
        self, bot_chat_id: int, bot_msg_id: int | None, ts: int | None, text: str
    ) -> tuple[int, int | None]:
        """Map a message seen by the bot to the owner's view of the same chat.

        Supergroup message ids are shared, so they map directly. In old-style basic
        groups each account sees its own ids, so the message is found by time + text.
        """
        if str(bot_chat_id).startswith("-100") or bot_msg_id is None:
            return bot_chat_id, bot_msg_id
        if not self.client or ts is None:
            return bot_chat_id, None
        entity = await self._entity(bot_chat_id)
        want = _norm(text)
        offset = datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(seconds=2)
        async for msg in self.client.iter_messages(entity, offset_date=offset, limit=30):
            if abs(int(msg.date.timestamp()) - ts) <= 1 and _norm(msg.message) == want:
                return bot_chat_id, msg.id
        return bot_chat_id, None

    async def send(self, peer_id: int, text: str, reply_to: int | None) -> None:
        if not self.ready or not self.client:
            raise RuntimeError("Send as me is not available (Telethon not logged in)")
        entity = await self._entity(peer_id)
        try:
            await self.client.send_message(entity, text, reply_to=reply_to)
        except RPCError:
            if reply_to is None:
                raise
            # The original may have been deleted; send without threading.
            await self.client.send_message(entity, text)
