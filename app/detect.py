"""Decide whether a group message is aimed at the owner."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Entity:
    type: str
    offset: int  # UTF-16 code units, as Telegram sends them
    length: int
    user_id: int | None = None


def entity_text(text: str, offset: int, length: int) -> str:
    """Slice `text` using Telegram's UTF-16 offsets (emoji take 2 units)."""
    encoded = text.encode("utf-16-le")
    return encoded[offset * 2 : (offset + length) * 2].decode("utf-16-le", errors="ignore")


def _alias_pattern(alias: str) -> re.Pattern[str]:
    escaped = re.escape(alias)
    # Word boundaries only make sense for scripts with spaces between words.
    # For CJK and similar, a plain substring match is the right behaviour.
    if re.search(r"[A-Za-z0-9]", alias[0]) and re.search(r"[A-Za-z0-9]", alias[-1]):
        return re.compile(rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])", re.IGNORECASE)
    return re.compile(escaped, re.IGNORECASE)


class MentionDetector:
    def __init__(self, owner_id: int, username: str | None, aliases: list[str]):
        self.owner_id = owner_id
        self.username = (username or "").lstrip("@").lower() or None
        self._alias_patterns = [_alias_pattern(a) for a in aliases if a.strip()]

    def reason(
        self,
        text: str,
        entities: list[Entity],
        reply_to_user_id: int | None,
        sender_id: int | None,
    ) -> str | None:
        """Return why the message targets the owner, or None if it doesn't."""
        if sender_id == self.owner_id:
            return None  # the owner's own messages never trigger

        if reply_to_user_id is not None and reply_to_user_id == self.owner_id:
            return "replied to your message"

        for ent in entities:
            if ent.type == "text_mention" and ent.user_id == self.owner_id:
                return "mentioned you"
            if ent.type == "mention" and self.username:
                handle = entity_text(text, ent.offset, ent.length).lstrip("@").lower()
                if handle == self.username:
                    return "mentioned @" + self.username

        for pat in self._alias_patterns:
            if pat.search(text):
                return "said your name"
        return None
