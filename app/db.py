"""Small SQLite store: rolling message buffer per group + suggestion records."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    chat_id       INTEGER NOT NULL,
    msg_id        INTEGER NOT NULL,
    sender_id     INTEGER,
    sender_name   TEXT,
    text          TEXT NOT NULL,
    ts            INTEGER NOT NULL,
    reply_to_id   INTEGER,
    is_owner      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_id, ts);

CREATE TABLE IF NOT EXISTS suggestions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    created            INTEGER NOT NULL,
    bot_id             INTEGER NOT NULL,
    source             TEXT NOT NULL,          -- 'group' or 'forward'
    chat_id            INTEGER,                -- where the reply should go (bot-api id), may be NULL
    chat_title         TEXT,
    target_msg_id      INTEGER,                -- message to reply to (user's view when from Telethon)
    target_ts          INTEGER,
    target_sender_id   INTEGER,
    target_sender_name TEXT,
    target_text        TEXT NOT NULL,
    context            TEXT NOT NULL,
    via_telethon       INTEGER NOT NULL DEFAULT 0, -- 1 = chat_id/target_msg_id are Telethon ids
    mode               TEXT NOT NULL,
    intent             TEXT,
    drafts             TEXT NOT NULL DEFAULT '[]',
    owner_msg_id       INTEGER,
    sent               INTEGER NOT NULL DEFAULT 0,
    reason             TEXT
);

CREATE TABLE IF NOT EXISTS pending_custom (
    bot_id         INTEGER NOT NULL,
    prompt_msg_id  INTEGER NOT NULL,
    suggestion_id  INTEGER NOT NULL,
    created        INTEGER NOT NULL,
    PRIMARY KEY (bot_id, prompt_msg_id)
);
"""


@dataclass
class StoredMessage:
    chat_id: int
    msg_id: int
    sender_id: int | None
    sender_name: str
    text: str
    ts: int
    reply_to_id: int | None
    is_owner: bool


@dataclass
class Suggestion:
    id: int
    bot_id: int
    source: str
    chat_id: int | None
    chat_title: str | None
    target_msg_id: int | None
    target_ts: int | None
    target_sender_id: int | None
    target_sender_name: str | None
    target_text: str
    context: str
    via_telethon: bool
    mode: str
    intent: str | None
    drafts: list[str]
    owner_msg_id: int | None
    sent: bool
    reason: str | None = None


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.commit()

    # ---- message buffer -------------------------------------------------

    def add_message(self, m: StoredMessage, keep: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?)",
                (m.chat_id, m.msg_id, m.sender_id, m.sender_name, m.text, m.ts,
                 m.reply_to_id, int(m.is_owner)),
            )
            # Trim the buffer for this chat to the newest `keep` messages.
            self._conn.execute(
                """DELETE FROM messages WHERE chat_id=? AND msg_id NOT IN (
                       SELECT msg_id FROM messages WHERE chat_id=? ORDER BY ts DESC, msg_id DESC LIMIT ?)""",
                (m.chat_id, m.chat_id, keep),
            )
            self._conn.commit()

    def recent_messages(self, chat_id: int, limit: int, up_to_msg_id: int | None = None) -> list[StoredMessage]:
        with self._lock:
            if up_to_msg_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM messages WHERE chat_id=? ORDER BY ts DESC, msg_id DESC LIMIT ?",
                    (chat_id, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM messages WHERE chat_id=? AND msg_id<=? ORDER BY ts DESC, msg_id DESC LIMIT ?",
                    (chat_id, up_to_msg_id, limit),
                ).fetchall()
        return [self._row_to_msg(r) for r in reversed(rows)]

    def get_message(self, chat_id: int, msg_id: int) -> StoredMessage | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE chat_id=? AND msg_id=?", (chat_id, msg_id)
            ).fetchone()
        return self._row_to_msg(row) if row else None

    def owner_spoke_after(self, chat_id: int, msg_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM messages WHERE chat_id=? AND msg_id>? AND is_owner=1 LIMIT 1",
                (chat_id, msg_id),
            ).fetchone()
        return row is not None

    @staticmethod
    def _row_to_msg(r: sqlite3.Row) -> StoredMessage:
        return StoredMessage(
            chat_id=r["chat_id"], msg_id=r["msg_id"], sender_id=r["sender_id"],
            sender_name=r["sender_name"] or "Unknown", text=r["text"], ts=r["ts"],
            reply_to_id=r["reply_to_id"], is_owner=bool(r["is_owner"]),
        )

    # ---- suggestions ----------------------------------------------------

    def create_suggestion(self, **fields) -> int:
        fields.setdefault("created", int(time.time()))
        fields["drafts"] = json.dumps(fields.get("drafts", []), ensure_ascii=False)
        fields["via_telethon"] = int(bool(fields.get("via_telethon", False)))
        cols = ",".join(fields)
        marks = ",".join("?" for _ in fields)
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO suggestions ({cols}) VALUES ({marks})", tuple(fields.values())
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def update_suggestion(self, sid: int, **fields) -> None:
        if "drafts" in fields:
            fields["drafts"] = json.dumps(fields["drafts"], ensure_ascii=False)
        if "sent" in fields:
            fields["sent"] = int(bool(fields["sent"]))
        sets = ",".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE suggestions SET {sets} WHERE id=?", (*fields.values(), sid))
            self._conn.commit()

    def get_suggestion(self, sid: int) -> Suggestion | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM suggestions WHERE id=?", (sid,)).fetchone()
        return self._row_to_sug(r) if r else None

    def suggestion_by_owner_msg(self, bot_id: int, owner_msg_id: int) -> Suggestion | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM suggestions WHERE bot_id=? AND owner_msg_id=? ORDER BY id DESC LIMIT 1",
                (bot_id, owner_msg_id),
            ).fetchone()
        return self._row_to_sug(r) if r else None

    @staticmethod
    def _row_to_sug(r: sqlite3.Row) -> Suggestion:
        return Suggestion(
            id=r["id"], bot_id=r["bot_id"], source=r["source"], chat_id=r["chat_id"],
            chat_title=r["chat_title"], target_msg_id=r["target_msg_id"], target_ts=r["target_ts"],
            target_sender_id=r["target_sender_id"], target_sender_name=r["target_sender_name"],
            target_text=r["target_text"], context=r["context"], via_telethon=bool(r["via_telethon"]),
            mode=r["mode"], intent=r["intent"], drafts=json.loads(r["drafts"] or "[]"),
            owner_msg_id=r["owner_msg_id"], sent=bool(r["sent"]), reason=r["reason"],
        )

    # ---- pending custom intents ----------------------------------------

    def add_pending_custom(self, bot_id: int, prompt_msg_id: int, sid: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO pending_custom VALUES (?,?,?,?)",
                (bot_id, prompt_msg_id, sid, int(time.time())),
            )
            self._conn.commit()

    def pop_pending_custom(self, bot_id: int, prompt_msg_id: int) -> int | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT suggestion_id FROM pending_custom WHERE bot_id=? AND prompt_msg_id=?",
                (bot_id, prompt_msg_id),
            ).fetchone()
            if r:
                self._conn.execute(
                    "DELETE FROM pending_custom WHERE bot_id=? AND prompt_msg_id=?",
                    (bot_id, prompt_msg_id),
                )
                self._conn.commit()
        return int(r[0]) if r else None

    def prune(self, older_than_days: int = 14) -> None:
        cutoff = int(time.time()) - older_than_days * 86400
        with self._lock:
            self._conn.execute("DELETE FROM suggestions WHERE created<?", (cutoff,))
            self._conn.execute("DELETE FROM pending_custom WHERE created<?", (cutoff,))
            self._conn.commit()
