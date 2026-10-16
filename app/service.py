"""Core logic shared by every bot: buffering, triggering, drafting and sending."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from . import formatting as fmt
from .config import BotConfig, Config
from .db import Store, Suggestion
from .llm import LLMClient, LLMError
from .me_client import MeClient
from .prompts import Prompts

log = logging.getLogger(__name__)


@dataclass
class PendingTrigger:
    bot_id: int
    chat_id: int
    chat_title: str
    msg_id: int
    ts: int
    sender_id: int | None
    sender_name: str
    text: str
    reason: str
    task: asyncio.Task | None = None


@dataclass
class ForwardBatch:
    bot_id: int
    items: list[dict] = field(default_factory=list)
    task: asyncio.Task | None = None


class Service:
    def __init__(self, cfg: Config, store: Store, llm: LLMClient, prompts: Prompts, me: MeClient):
        self.cfg = cfg
        self.store = store
        self.llm = llm
        self.prompts = prompts
        self.me = me
        self.bots: dict[int, Bot] = {}
        self.bot_cfgs: dict[int, BotConfig] = {}
        self._pending: dict[tuple[int, int], PendingTrigger] = {}
        self._forwards: dict[int, ForwardBatch] = {}
        self._notified_unlisted: set[tuple[int, int]] = set()
        self._llm_lock = asyncio.Semaphore(2)  # don't flood a small GPU

    def register_bot(self, bot: Bot, bcfg: BotConfig) -> None:
        self.bots[bcfg.bot_id] = bot
        self.bot_cfgs[bcfg.bot_id] = bcfg

    def allowed(self, bot_id: int, chat_id: int) -> bool:
        bcfg = self.bot_cfgs.get(bot_id)
        return bool(bcfg and chat_id in bcfg.groups)

    # ---- group triggers (debounced) ---------------------------------------

    def on_group_activity(self, bot_id: int, chat_id: int, sender_id: int | None, is_owner: bool) -> None:
        key = (bot_id, chat_id)
        pending = self._pending.get(key)
        if not pending:
            return
        if is_owner:
            # You answered yourself, so no suggestion is needed.
            if pending.task:
                pending.task.cancel()
            self._pending.pop(key, None)
        elif sender_id == pending.sender_id:
            self._schedule(key)  # they're still typing: wait a little longer

    def trigger(self, pt: PendingTrigger) -> None:
        key = (pt.bot_id, pt.chat_id)
        old = self._pending.get(key)
        if old and old.task:
            old.task.cancel()
        self._pending[key] = pt
        self._schedule(key)

    def _schedule(self, key: tuple[int, int]) -> None:
        pt = self._pending.get(key)
        if not pt:
            return
        if pt.task:
            pt.task.cancel()
        pt.task = asyncio.create_task(self._fire_after_delay(key))

    async def _fire_after_delay(self, key: tuple[int, int]) -> None:
        try:
            await asyncio.sleep(self.cfg.debounce_seconds)
        except asyncio.CancelledError:
            return
        pt = self._pending.pop(key, None)
        if not pt:
            return
        if self.store.owner_spoke_after(pt.chat_id, pt.msg_id):
            return
        try:
            await self._start_group_suggestion(pt)
        except Exception:
            log.exception("Failed to create suggestion for chat %s", pt.chat_id)

    def _group_context(self, chat_id: int, target_msg_id: int) -> str:
        msgs = self.store.recent_messages(chat_id, self.cfg.context_messages)
        lines = []
        for m in msgs:
            if m.msg_id == target_msg_id:
                continue
            who = "Me" if m.is_owner else m.sender_name
            lines.append(f"{who}: {m.text}")
        return "\n".join(lines)

    async def _start_group_suggestion(self, pt: PendingTrigger) -> None:
        sid = self.store.create_suggestion(
            bot_id=pt.bot_id, source="group", chat_id=pt.chat_id, chat_title=pt.chat_title,
            target_msg_id=pt.msg_id, target_ts=pt.ts, target_sender_id=pt.sender_id,
            target_sender_name=pt.sender_name, target_text=pt.text,
            context=self._group_context(pt.chat_id, pt.msg_id), via_telethon=False,
            mode="reply", reason=pt.reason,
        )
        await self._post_new(sid)

    # ---- forwarded messages (batched) ------------------------------------

    def add_forward(self, bot_id: int, item: dict) -> None:
        batch = self._forwards.get(bot_id)
        if batch is None:
            batch = ForwardBatch(bot_id=bot_id)
            self._forwards[bot_id] = batch
        batch.items.append(item)
        if batch.task:
            batch.task.cancel()
        batch.task = asyncio.create_task(self._flush_forwards_after_delay(bot_id))

    async def _flush_forwards_after_delay(self, bot_id: int) -> None:
        try:
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            return
        batch = self._forwards.pop(bot_id, None)
        if not batch or not batch.items:
            return
        try:
            await self._start_forward_suggestion(bot_id, batch.items)
        except Exception:
            log.exception("Failed to handle forwarded message")
            await self.notify_owner(bot_id, "⚠️ Something went wrong handling that forward. Check the logs.")

    async def _start_forward_suggestion(self, bot_id: int, items: list[dict]) -> None:
        target = items[-1]
        earlier = items[:-1]
        chat_id = None
        target_msg_id = None
        chat_title = target["sender_name"]
        context_lines = [f"{it['sender_name']}: {it['text']}" for it in earlier]
        note = None
        sender_name = target["sender_name"]

        located = None
        if self.me.ready:
            located = await self.me.locate_forward(target["text"], target["ts"], target["sender_id"])
        if located:
            chat_id = located.peer_id
            target_msg_id = located.msg_id
            chat_title = located.chat_title
            sender_name = located.sender_name
            history = await self.me.context_lines(located.peer_id, located.msg_id, self.cfg.context_messages + 1)
            if history and history[-1].endswith(located.text):
                history = history[:-1]
            if history:
                context_lines = history
        elif self.me.ready and target["sender_id"]:
            chat_id = target["sender_id"]  # can still message them, just not as a threaded reply
            note = "<i>Couldn't find the original message, so a sent reply won't be threaded.</i>"
        elif self.me.ready:
            note = "<i>Couldn't find the original chat (sender hides forwards), so copy the draft manually.</i>"

        sid = self.store.create_suggestion(
            bot_id=bot_id, source="forward", chat_id=chat_id, chat_title=chat_title,
            target_msg_id=target_msg_id, target_ts=target["ts"], target_sender_id=target["sender_id"],
            target_sender_name=sender_name, target_text=target["text"],
            context="\n".join(context_lines), via_telethon=chat_id is not None,
            mode="reply",
        )
        await self._post_new(sid, note=note)

    # ---- drafting ---------------------------------------------------------

    async def _post_new(self, sid: int, note: str | None = None) -> None:
        s = self.store.get_suggestion(sid)
        assert s
        bot = self.bots[s.bot_id]
        try:
            msg = await bot.send_message(self.cfg.owner_id, fmt.drafting(s), parse_mode="HTML")
        except TelegramForbiddenError:
            log.error(
                "Can't message you: open a private chat with this bot and press Start once (bot id %s)",
                s.bot_id,
            )
            return
        self.store.update_suggestion(sid, owner_msg_id=msg.message_id)
        await self.redraft(sid, note=note)

    async def redraft(self, sid: int, mode: str | None = None, intent: str | None = None,
                      fresh: bool = False, note: str | None = None) -> None:
        s = self.store.get_suggestion(sid)
        if not s:
            return
        updates: dict = {}
        if mode:
            updates["mode"] = mode
        if intent is not None:
            updates["intent"] = intent
        if mode and mode != "custom":
            updates["intent"] = None
        if updates:
            self.store.update_suggestion(sid, **updates)
            s = self.store.get_suggestion(sid)
            assert s
        previous = s.drafts if fresh else None
        await self._edit(s, fmt.drafting(s), None)

        system, user = self.prompts.build(
            mode=s.mode, context=s.context, target_sender=s.target_sender_name or "them",
            target_text=s.target_text, chat_title=s.chat_title, intent=s.intent,
            draft_count=self.cfg.draft_count, previous_drafts=previous,
        )
        try:
            async with self._llm_lock:
                drafts = await self.llm.draft(system, user, self.cfg.draft_count)
        except LLMError as exc:
            self.store.update_suggestion(sid, drafts=[])
            s = self.store.get_suggestion(sid)
            assert s
            await self._edit(s, fmt.body(s, note=f"⚠️ {escape(str(exc))}"), fmt.retry_keyboard(s))
            return
        self.store.update_suggestion(sid, drafts=drafts)
        s = self.store.get_suggestion(sid)
        assert s
        await self._edit(s, fmt.body(s, note=note), fmt.keyboard(s, self.me.ready))

    async def _edit(self, s: Suggestion, text: str, markup) -> None:
        if not s.owner_msg_id:
            return
        bot = self.bots[s.bot_id]
        try:
            await bot.edit_message_text(
                text=text, chat_id=self.cfg.owner_id, message_id=s.owner_msg_id,
                parse_mode="HTML", reply_markup=markup,
            )
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc):
                return
            log.warning("Could not update suggestion message: %s", exc)

    async def refresh_view(self, sid: int, note: str | None = None, markup_override=None) -> None:
        s = self.store.get_suggestion(sid)
        if not s:
            return
        markup = markup_override if markup_override is not None else (
            fmt.keyboard(s, self.me.ready) if not s.sent else None
        )
        await self._edit(s, fmt.body(s, note=note), markup)

    # ---- sending as the owner --------------------------------------------

    async def send_as_me(self, sid: int, text: str) -> str | None:
        """Send `text` as the owner. Returns an error message, or None on success."""
        s = self.store.get_suggestion(sid)
        if not s:
            return "That suggestion has expired."
        if not self.me.ready:
            return "Send as me isn't set up (Telethon not logged in)."
        if s.chat_id is None:
            return "I don't know which chat to send this to. Copy the draft instead."
        try:
            if s.via_telethon:
                peer, reply_to = s.chat_id, s.target_msg_id
            else:
                peer, reply_to = await self.me.resolve_group_reply(
                    s.chat_id, s.target_msg_id, s.target_ts, s.target_text
                )
            await self.me.send(peer, text, reply_to)
        except Exception as exc:
            log.exception("Send as me failed")
            return f"Sending failed: {exc.__class__.__name__}"
        self.store.update_suggestion(sid, sent=True)
        return None

    # ---- misc -------------------------------------------------------------

    async def notify_owner(self, bot_id: int, text: str) -> None:
        bot = self.bots.get(bot_id)
        if not bot:
            return
        try:
            await bot.send_message(self.cfg.owner_id, text, parse_mode="HTML")
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            log.warning("Could not notify owner: %s", exc)

    async def unlisted_group(self, bot: Bot, chat_id: int, title: str) -> None:
        key = (bot.id, chat_id)
        if key in self._notified_unlisted:
            return
        self._notified_unlisted.add(key)
        bcfg = self.bot_cfgs.get(bot.id)
        idx = bcfg.index if bcfg else "N"
        log.info("Ignoring unlisted group %r (id %s)", title, chat_id)
        if self.cfg.leave_unlisted_groups:
            try:
                await bot.leave_chat(chat_id)
            except Exception:
                pass
            await self.notify_owner(
                bot.id, f"🚪 Left unlisted group <b>{escape(title)}</b> (<code>{chat_id}</code>)."
            )
        else:
            await self.notify_owner(
                bot.id,
                f"ℹ️ I'm in <b>{escape(title)}</b> but it's not in my list, so I'm ignoring it.\n"
                f"To enable it, add <code>{chat_id}</code> to <code>BOT_{idx}_GROUPS</code> and restart.",
            )
