"""Prompt templates. Defaults live here; personal details go in a prompts.yaml
on the server (see prompts.example.yaml), which overrides any key below."""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

MODES = ("reply", "ack", "gather", "custom")

MODE_LABELS = {
    "reply": "💬 Reply",
    "ack": "👍 Acknowledge",
    "gather": "🔎 Gather info",
    "custom": "✍️ Custom",
}

DEFAULTS: dict[str, str] = {
    "about_me": (
        "I am the person whose replies you draft. Assume a professional who works "
        "with colleagues and clients over chat."
    ),
    "style": (
        "Polite, warm and concise. Sound like a real person typing in a chat app, "
        "not an email. No greetings like 'Dear', no sign-offs, no emojis unless the "
        "conversation already uses them."
    ),
    "rules": (
        "Reply in the same language (or language mix) the other person used. "
        "Never invent facts, dates, prices or commitments that are not in the "
        "conversation or in my instructions. If something is unknown, say it will be "
        "checked rather than making it up. Keep each draft short enough for a chat message."
    ),
    "mode_reply": (
        "Draft a natural, helpful reply to the target message, taking the whole "
        "conversation into account."
    ),
    "mode_ack": (
        "Draft an acknowledgement that shows I understood the specific request: restate "
        "the key point in my own words (what is needed, for whom, by when if mentioned) "
        "and say what happens next (e.g. I will check and share an ETA). Do NOT write "
        "generic replies like 'noted', 'ok', 'will update' on their own."
    ),
    "mode_gather": (
        "The message reports a problem or request without enough detail. Draft a reply "
        "that asks only the questions a competent, experienced engineer would ask for "
        "THIS specific problem, to diagnose or act on it. Skip trivial checks that do not "
        "fit the context (e.g. 'is it plugged in' for a cloud server). Ask at most 3-5 "
        "focused questions, grouped naturally, and keep it friendly."
    ),
    "mode_custom": (
        "Draft a reply that says what I intend to say (my intent is given below), "
        "phrased politely and fitting the conversation."
    ),
}


class Prompts:
    def __init__(self, path: Path | None):
        self.values = dict(DEFAULTS)
        self.path = path
        self.reload()

    def reload(self) -> None:
        values = dict(DEFAULTS)
        if self.path and self.path.exists():
            try:
                loaded = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
                if not isinstance(loaded, dict):
                    raise ValueError("top level must be a mapping")
                for key, val in loaded.items():
                    if key in DEFAULTS and isinstance(val, str) and val.strip():
                        values[key] = val.strip()
                    elif key not in DEFAULTS:
                        log.warning("prompts file: unknown key %r ignored", key)
                log.info("Loaded prompts from %s", self.path)
            except Exception as exc:  # keep running on a bad file
                log.error("Could not read prompts file %s: %s (using defaults)", self.path, exc)
        self.values = values

    def build(
        self,
        mode: str,
        context: str,
        target_sender: str,
        target_text: str,
        chat_title: str | None,
        intent: str | None,
        draft_count: int,
        previous_drafts: list[str] | None = None,
    ) -> tuple[str, str]:
        """Return (system_prompt, user_prompt)."""
        v = self.values
        system = (
            "You draft chat replies on my behalf. I will review and send them myself.\n\n"
            f"About me:\n{v['about_me']}\n\n"
            f"My style:\n{v['style']}\n\n"
            f"Rules:\n{v['rules']}\n\n"
            f"Respond ONLY with JSON of the form {{\"drafts\": [\"...\", \"...\"]}} containing "
            f"exactly {draft_count} different drafts. No markdown, no explanation."
        )
        task = v.get(f"mode_{mode}", v["mode_reply"])
        parts = []
        if chat_title:
            parts.append(f"Chat: {chat_title}")
        parts.append("Conversation so far (oldest first; 'Me' is me):\n" + (context or "(no earlier messages)"))
        parts.append(f"Target message from {target_sender}:\n{target_text}")
        parts.append(f"Task: {task}")
        if intent:
            parts.append(f"My intent: {intent}")
        if previous_drafts:
            joined = "\n".join(f"- {d}" for d in previous_drafts)
            parts.append(f"Give fresh alternatives, different from these earlier drafts:\n{joined}")
        return system, "\n\n".join(parts)
