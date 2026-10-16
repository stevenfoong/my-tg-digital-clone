"""Telegram update handlers (shared by every bot)."""
from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ForceReply,
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
)

from . import formatting as fmt
from .db import StoredMessage
from .detect import Entity, MentionDetector
from .prompts import MODES
from .service import PendingTrigger, Service

log = logging.getLogger(__name__)

GROUP_TYPES = {"group", "supergroup"}

HELP = (
    "<b>Reply assistant</b>\n\n"
    "• When someone mentions you in a watched group, I send you draft replies here.\n"
    "• Forward me any message and I'll draft a reply to it.\n"
    "• Use the buttons to switch between Reply / Acknowledge / Gather info, or tap Custom "
    "(or reply to my message) to tell me what you want to say.\n"
    "• Reply to a suggestion with <code>/send your text</code> to send exactly that as you.\n\n"
    "Commands: /status · /reload (re-read prompts.yaml)"
)


def _text_of(message: Message) -> str:
    if message.text:
        return message.text
    if message.caption:
        return message.caption
    kind = message.content_type.replace("_", " ") if message.content_type else "message"
    return f"[{kind}]"


def _entities(message: Message) -> list[Entity]:
    raw = message.entities or message.caption_entities or []
    return [
        Entity(type=e.type, offset=e.offset, length=e.length, user_id=e.user.id if e.user else None)
        for e in raw
    ]


def _sender_name(message: Message) -> str:
    if message.from_user:
        return message.from_user.full_name
    if message.sender_chat:
        return message.sender_chat.title or "Channel"
    return "Unknown"


def build_router(service: Service, detector: MentionDetector) -> Router:
    router = Router()
    cfg = service.cfg
    owner = cfg.owner_id

    def is_owner(user) -> bool:
        return bool(user and user.id == owner)

    # ---------------- private chat with the owner ----------------

    @router.message(F.chat.type == "private", Command("start", "help"))
    async def cmd_start(message: Message) -> None:
        if not is_owner(message.from_user):
            return  # private assistant: stay silent for everyone else
        await message.answer(HELP, parse_mode="HTML")

    @router.message(F.chat.type == "private", Command("status"))
    async def cmd_status(message: Message, bot: Bot) -> None:
        if not is_owner(message.from_user):
            return
        bcfg = service.bot_cfgs.get(bot.id)
        groups = ", ".join(f"<code>{g}</code>" for g in (bcfg.groups if bcfg else [])) or "none"
        me_state = "ready ✅" if service.me.ready else (
            "enabled but not logged in ⚠️" if cfg.telethon.enabled else "off"
        )
        text = (
            f"<b>Status</b>\n"
            f"Bots running: {len(service.bots)}\n"
            f"This bot watches: {groups}\n"
            f"Model: <code>{escape(cfg.llm.model)}</code> ({cfg.llm.api})\n"
            f"Send as me: {me_state}\n"
            f"Prompts file: {'loaded' if cfg.prompts_file and cfg.prompts_file.exists() else 'defaults'}"
        )
        await message.answer(text, parse_mode="HTML")

    @router.message(F.chat.type == "private", Command("reload"))
    async def cmd_reload(message: Message) -> None:
        if not is_owner(message.from_user):
            return
        service.prompts.reload()
        await message.answer("Prompts reloaded.")

    @router.message(F.chat.type == "private", Command("send"))
    async def cmd_send(message: Message, bot: Bot) -> None:
        if not is_owner(message.from_user):
            return
        text = (message.text or "").split(maxsplit=1)
        body = text[1].strip() if len(text) > 1 else ""
        reply = message.reply_to_message
        if not reply or not body:
            await message.answer("Reply to one of my suggestions with <code>/send your text</code>.",
                                 parse_mode="HTML")
            return
        s = service.store.suggestion_by_owner_msg(bot.id, reply.message_id)
        if not s:
            await message.answer("That isn't one of my suggestions.")
            return
        err = await service.send_as_me(s.id, body)
        if err:
            await message.answer(f"⚠️ {err}")
            return
        await message.answer("✅ Sent.")
        await service.refresh_view(s.id, note="✅ <i>Sent your own text.</i>")

    @router.message(F.chat.type == "private", F.forward_origin)
    async def on_forward(message: Message, bot: Bot) -> None:
        if not is_owner(message.from_user):
            return
        text = message.text or message.caption
        if not text:
            await message.answer("I can only draft replies to messages with text.")
            return
        origin = message.forward_origin
        sender_id = None
        sender_name = "Someone"
        if isinstance(origin, MessageOriginUser):
            sender_id = origin.sender_user.id
            sender_name = origin.sender_user.full_name
            if sender_id == owner:
                sender_name = "Me"
        elif isinstance(origin, MessageOriginHiddenUser):
            sender_name = origin.sender_user_name
        elif isinstance(origin, (MessageOriginChat,)):
            sender_name = origin.sender_chat.title or "Group"
        elif isinstance(origin, MessageOriginChannel):
            sender_name = origin.chat.title or "Channel"
        service.add_forward(bot.id, {
            "text": text,
            "ts": int(origin.date.timestamp()),
            "sender_id": sender_id,
            "sender_name": sender_name,
        })

    @router.message(F.chat.type == "private", F.reply_to_message, F.text)
    async def on_owner_reply(message: Message, bot: Bot) -> None:
        if not is_owner(message.from_user):
            return
        replied = message.reply_to_message
        sid = service.store.pop_pending_custom(bot.id, replied.message_id)
        if sid is None:
            s = service.store.suggestion_by_owner_msg(bot.id, replied.message_id)
            sid = s.id if s else None
        if sid is None:
            await message.answer("Reply to one of my suggestions to give a custom instruction.")
            return
        await service.redraft(sid, mode="custom", intent=message.text.strip())

    @router.message(F.chat.type == "private")
    async def on_private_other(message: Message) -> None:
        if not is_owner(message.from_user):
            return
        await message.answer(
            "Forward me a message to get drafts, or reply to one of my suggestions. /help",
        )

    # ---------------- buttons ----------------

    @router.callback_query()
    async def on_button(query: CallbackQuery, bot: Bot) -> None:
        if not is_owner(query.from_user):
            await query.answer()
            return
        data = (query.data or "").split(":")
        if len(data) < 2 or not data[1].isdigit():
            await query.answer()
            return
        action, sid = data[0], int(data[1])
        s = service.store.get_suggestion(sid)
        if not s:
            await query.answer("This suggestion has expired.", show_alert=True)
            return

        if action == "m" and len(data) == 3 and data[2] in MODES:
            await query.answer()
            await service.redraft(sid, mode=data[2])
        elif action == "r":
            await query.answer("Regenerating…")
            await service.redraft(sid, fresh=bool(s.drafts))
        elif action == "k":
            await query.answer()
            prompt = await bot.send_message(
                owner,
                "✍️ What do you want to say? Reply to this message (e.g. "
                "<i>say yes but only next week</i>).",
                parse_mode="HTML",
                reply_markup=ForceReply(input_field_placeholder="What should the reply say?"),
            )
            service.store.add_pending_custom(bot.id, prompt.message_id, sid)
        elif action == "s" and len(data) == 3:
            idx = int(data[2])
            if s.sent or not (1 <= idx <= len(s.drafts)):
                await query.answer("Nothing to send.")
                return
            await query.answer()
            await service.refresh_view(sid, markup_override=fmt.confirm_keyboard(s, idx))
        elif action == "y" and len(data) == 3:
            idx = int(data[2])
            if s.sent or not (1 <= idx <= len(s.drafts)):
                await query.answer("Already sent.")
                return
            await query.answer("Sending…")
            err = await service.send_as_me(sid, s.drafts[idx - 1])
            if err:
                await service.refresh_view(sid, note=f"⚠️ {escape(err)}")
            else:
                await service.refresh_view(sid, note=f"✅ <i>Sent draft {idx}.</i>")
        elif action == "n":
            await query.answer("Cancelled")
            await service.refresh_view(sid)
        else:
            await query.answer()

    # ---------------- groups ----------------

    @router.my_chat_member(F.chat.type.in_(GROUP_TYPES))
    async def on_added(update: ChatMemberUpdated, bot: Bot) -> None:
        status = update.new_chat_member.status
        if status in ("member", "administrator") and not service.allowed(bot.id, update.chat.id):
            await service.unlisted_group(bot, update.chat.id, update.chat.title or "a group")

    @router.message(F.chat.type.in_(GROUP_TYPES))
    async def on_group_message(message: Message, bot: Bot) -> None:
        chat = message.chat
        if not service.allowed(bot.id, chat.id):
            await service.unlisted_group(bot, chat.id, chat.title or "a group")
            return
        if message.new_chat_members or message.left_chat_member:
            return

        sender_id = message.from_user.id if message.from_user else None
        owner_msg = sender_id == owner
        text = _text_of(message)
        reply_to = message.reply_to_message
        service.store.add_message(
            StoredMessage(
                chat_id=chat.id, msg_id=message.message_id, sender_id=sender_id,
                sender_name=_sender_name(message), text=text, ts=int(message.date.timestamp()),
                reply_to_id=reply_to.message_id if reply_to else None, is_owner=owner_msg,
            ),
            keep=cfg.buffer_limit,
        )
        service.on_group_activity(bot.id, chat.id, sender_id, owner_msg)
        if owner_msg or (message.from_user and message.from_user.is_bot):
            return

        reply_user = reply_to.from_user.id if reply_to and reply_to.from_user else None
        if reply_to and reply_to.forum_topic_created:
            reply_user = None  # in forum topics every message "replies" to the topic header
        reason = detector.reason(text, _entities(message), reply_user, sender_id)
        if not reason:
            return
        service.trigger(PendingTrigger(
            bot_id=bot.id, chat_id=chat.id, chat_title=chat.title or "Group",
            msg_id=message.message_id, ts=int(message.date.timestamp()),
            sender_id=sender_id, sender_name=_sender_name(message), text=text, reason=reason,
        ))

    return router
