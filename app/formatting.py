"""Builds the private message the owner receives, and its buttons."""
from __future__ import annotations

from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .db import Suggestion
from .prompts import MODE_LABELS

TG_LIMIT = 4096


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def header(s: Suggestion) -> str:
    who = escape(s.target_sender_name or "Someone")
    where = escape(s.chat_title or "a chat")
    if s.source == "group":
        top = f"🔔 <b>{where}</b>\n{who} {escape(s.reason or 'mentioned you')}:"
    else:
        top = f"📨 <b>{where}</b>\nForwarded from {who}:"
    quote = escape(_clip(s.target_text, 700))
    return f"{top}\n<blockquote>{quote}</blockquote>"


def body(s: Suggestion, note: str | None = None) -> str:
    parts = [header(s)]
    label = MODE_LABELS.get(s.mode, s.mode)
    if s.intent:
        parts.append(f"<b>{label}</b> · <i>{escape(_clip(s.intent, 200))}</i>")
    else:
        parts.append(f"<b>{label}</b>")
    if s.drafts:
        for i, d in enumerate(s.drafts, 1):
            parts.append(f"{i}. <code>{escape(d)}</code>")
        parts.append(
            "<i>Tap a draft to copy it. Reply to this message with what you want to say "
            "for a custom draft, or reply with</i> <code>/send your text</code> "
            "<i>to send exactly that.</i>"
        )
    if note:
        parts.append(note)
    text = "\n\n".join(parts)
    if len(text) > TG_LIMIT:
        text = text[: TG_LIMIT - 1] + "…"
    return text


def drafting(s: Suggestion) -> str:
    label = MODE_LABELS.get(s.mode, s.mode)
    return f"{header(s)}\n\n⏳ <i>Drafting ({label})…</i>"


def choose(s: Suggestion, note: str | None = None) -> str:
    """First message: no AI call yet, the owner picks how to reply."""
    parts = [
        header(s),
        "<b>How do you want to reply?</b>\n"
        "<i>Pick a style below, or reply to this message with what you want to say "
        "(e.g. <code>will check tonight</code>) to get custom drafts straight away. "
        "Reply with</i> <code>/send your text</code> <i>to send exactly that.</i>",
    ]
    if note:
        parts.append(note)
    text = "\n\n".join(parts)
    return text if len(text) <= TG_LIMIT else text[: TG_LIMIT - 1] + "…"


def choose_keyboard(s: Suggestion) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=MODE_LABELS["reply"], callback_data=f"m:{s.id}:reply"),
            InlineKeyboardButton(text=MODE_LABELS["ack"], callback_data=f"m:{s.id}:ack"),
        ],
        [
            InlineKeyboardButton(text=MODE_LABELS["gather"], callback_data=f"m:{s.id}:gather"),
            InlineKeyboardButton(text=MODE_LABELS["custom"], callback_data=f"k:{s.id}"),
        ],
    ])


def can_send(s: Suggestion, me_ready: bool) -> bool:
    return me_ready and s.chat_id is not None and not s.sent


def keyboard(s: Suggestion, me_ready: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if s.drafts and can_send(s, me_ready):
        rows.append([
            InlineKeyboardButton(text=f"📤 Send {i}", callback_data=f"s:{s.id}:{i}")
            for i in range(1, len(s.drafts) + 1)
        ])
    modes = []
    for mode in ("reply", "ack", "gather"):
        if mode != s.mode:
            modes.append(InlineKeyboardButton(text=MODE_LABELS[mode], callback_data=f"m:{s.id}:{mode}"))
    modes.append(InlineKeyboardButton(text=MODE_LABELS["custom"], callback_data=f"k:{s.id}"))
    rows.append(modes[:2])
    rows.append(modes[2:] + [InlineKeyboardButton(text="🔄 Regenerate", callback_data=f"r:{s.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_keyboard(s: Suggestion, idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Confirm send {idx}", callback_data=f"y:{s.id}:{idx}"),
        InlineKeyboardButton(text="✖ Cancel", callback_data=f"n:{s.id}"),
    ]])


def retry_keyboard(s: Suggestion) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Retry", callback_data=f"r:{s.id}"),
        InlineKeyboardButton(text=MODE_LABELS["custom"], callback_data=f"k:{s.id}"),
    ]])
