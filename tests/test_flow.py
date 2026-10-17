"""End-to-end flow with a fake Telegram API and a fake model."""
import asyncio
import datetime as dt
import itertools

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageText,
    GetChat,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import Chat, ChatFullInfo, Message, Update

from app.config import BotConfig, Config, LLMConfig, TelethonConfig
from app.db import Store
from app.detect import MentionDetector
from app.handlers import build_router
from app.prompts import Prompts
from app.service import Service

OWNER = 111
GROUP = -1001234
BOT_TOKEN = "42:TEST"


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self._ids = itertools.count(500)

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=next(self._ids), date=dt.datetime.now(),
                chat=Chat(id=method.chat_id, type="private"), text=method.text,
            )
        if isinstance(method, (EditMessageText, AnswerCallbackQuery)):
            return True
        if isinstance(method, GetChat):
            # model_construct skips validation, so this keeps working as the
            # Bot API adds required fields to ChatFullInfo.
            return ChatFullInfo.model_construct(
                id=method.chat_id, type="supergroup", title=f"Group {method.chat_id}",
            )
        return True

    async def close(self):
        pass

    async def stream_content(self, *a, **k):  # pragma: no cover
        yield b""

    def of(self, kind):
        return [c for c in self.calls if isinstance(c, kind)]


class FakeLLM:
    def __init__(self):
        self.prompts = []

    async def draft(self, system, user, expected):
        self.prompts.append((system, user))
        return [f"draft {i}" for i in range(1, expected + 1)]

    async def close(self):
        pass


class FakeMe:
    def __init__(self, ready=True):
        self.ready = ready
        self.sent = []

    async def resolve_group_reply(self, chat_id, msg_id, ts, text):
        return chat_id, msg_id

    async def send(self, peer, text, reply_to):
        self.sent.append((peer, text, reply_to))

    async def locate_forward(self, text, ts, sender_id):
        return None

    async def context_lines(self, *a):
        return []


def make_cfg(tmp_path):
    return Config(
        bots=[BotConfig(index=1, token=BOT_TOKEN, groups=[GROUP])],
        owner_id=OWNER, owner_username="steven", owner_aliases=["Steven"],
        llm=LLMConfig("ollama", "https://x", "m", 5, {}, None, 0.5),
        telethon=TelethonConfig(False, None, None, "x"),
        data_dir=tmp_path, prompts_file=None, context_messages=30, debounce_seconds=0,
        draft_count=3, leave_unlisted_groups=False, auto_draft=True, buffer_limit=200, log_level="INFO",
        all_groups={GROUP},
    )


_uid = itertools.count(1)


def group_msg(mid, user_id, name, text, chat_id=GROUP, reply_to=None):
    data = {
        "message_id": mid, "date": int(dt.datetime.now().timestamp()),
        "chat": {"id": chat_id, "type": "supergroup", "title": "Ops Team"},
        "from": {"id": user_id, "is_bot": False, "first_name": name},
        "text": text,
    }
    if reply_to:
        data["reply_to_message"] = reply_to
    return Update.model_validate({"update_id": next(_uid), "message": data})


def private_msg(text, reply_to_id=None, forward=None):
    data = {
        "message_id": next(_uid) + 10_000, "date": int(dt.datetime.now().timestamp()),
        "chat": {"id": OWNER, "type": "private"},
        "from": {"id": OWNER, "is_bot": False, "first_name": "Steven"},
        "text": text,
    }
    if text.startswith("/"):
        data["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    if reply_to_id:
        data["reply_to_message"] = {
            "message_id": reply_to_id, "date": data["date"],
            "chat": {"id": OWNER, "type": "private"},
            "from": {"id": 42, "is_bot": True, "first_name": "Bot"}, "text": "x",
        }
    if forward:
        data["forward_origin"] = forward
    return Update.model_validate({"update_id": next(_uid), "message": data})


def button(data):
    return Update.model_validate({"update_id": next(_uid), "callback_query": {
        "id": str(next(_uid)), "chat_instance": "c", "data": data,
        "from": {"id": OWNER, "is_bot": False, "first_name": "Steven"},
    }})


@pytest.fixture
def env(tmp_path):
    cfg = make_cfg(tmp_path)
    session = FakeSession()
    bot = Bot(BOT_TOKEN, session=session)
    llm, me = FakeLLM(), FakeMe()
    service = Service(cfg, Store(tmp_path / "db.sqlite"), llm, Prompts(None), me)
    service.register_bot(bot, cfg.bots[0])
    dp = Dispatcher()
    dp.include_router(build_router(service, MentionDetector(OWNER, "steven", ["Steven"])))

    async def feed(update):
        await dp.feed_update(bot, update)
        await asyncio.sleep(0.05)  # let debounce / batch tasks run

    return cfg, session, bot, llm, me, service, feed


async def test_group_mention_to_send_as_me(env):
    cfg, session, bot, llm, me, service, feed = env
    await feed(group_msg(1, 5, "Ann", "morning all"))
    await feed(group_msg(2, 6, "Ben", "Steven the AWS server is down"))

    sends = session.of(SendMessage)
    assert len(sends) == 1 and sends[0].chat_id == OWNER
    system, user = llm.prompts[0]
    assert "Ann: morning all" in user and "AWS server is down" in user

    final = session.of(EditMessageText)[-1]
    assert "draft 1" in final.text and "Ops Team" in final.text
    labels = [b.text for row in final.reply_markup.inline_keyboard for b in row]
    assert "📤 Send 2" in labels

    s = service.store.suggestion_by_owner_msg(bot.id, sends[0].__dict__.get("message_id", 500)) \
        or service.store.get_suggestion(1)
    await feed(button(f"s:{s.id}:2"))
    confirm = session.of(EditMessageText)[-1]
    assert "Confirm send 2" in confirm.reply_markup.inline_keyboard[0][0].text
    await feed(button(f"y:{s.id}:2"))
    assert me.sent == [(GROUP, "draft 2", 2)]
    assert "Sent draft 2" in session.of(EditMessageText)[-1].text
    assert session.of(EditMessageText)[-1].reply_markup is None


async def test_owner_answering_cancels_suggestion(env):
    cfg, session, bot, llm, me, service, feed = env
    cfg.debounce_seconds = 1
    await feed(group_msg(1, 6, "Ben", "@steven can you check"))
    await feed(group_msg(2, OWNER, "Steven", "on it"))
    await asyncio.sleep(1.2)
    assert session.of(SendMessage) == []


async def test_reply_to_owner_triggers_and_unlisted_group_notifies(env):
    cfg, session, bot, llm, me, service, feed = env
    original = {"message_id": 1, "date": 1, "chat": {"id": GROUP, "type": "supergroup"},
                "from": {"id": OWNER, "is_bot": False, "first_name": "S"}, "text": "done"}
    await feed(group_msg(3, 6, "Ben", "thanks, one more thing", reply_to=original))
    assert len(session.of(SendMessage)) == 1

    await feed(group_msg(1, 6, "Ben", "Steven hi", chat_id=-100999))
    notes = [m.text for m in session.of(SendMessage) if "not in my list" in m.text]
    assert notes and "-100999" in notes[0]
    await feed(group_msg(2, 6, "Ben", "Steven again", chat_id=-100999))
    assert len([m for m in session.of(SendMessage) if "not in my list" in m.text]) == 1


async def test_modes_custom_and_send_command(env):
    cfg, session, bot, llm, me, service, feed = env
    await feed(group_msg(1, 6, "Ben", "Steven server down"))
    s = service.store.get_suggestion(1)

    await feed(button(f"m:{s.id}:gather"))
    assert "competent" in llm.prompts[-1][1]

    await feed(button(f"k:{s.id}"))
    prompt_msg = session.of(SendMessage)[-1]
    assert "What do you want to say" in prompt_msg.text
    prompt_id = 500 + len(session.of(SendMessage)) - 1
    await feed(private_msg("say we will fix it tonight", reply_to_id=prompt_id))
    assert "My intent: say we will fix it tonight" in llm.prompts[-1][1]
    assert service.store.get_suggestion(s.id).mode == "custom"

    owner_msg_id = service.store.get_suggestion(s.id).owner_msg_id
    await feed(private_msg("/send Fixing it now, give me 10 min", reply_to_id=owner_msg_id))
    assert me.sent[-1] == (GROUP, "Fixing it now, give me 10 min", 1)


async def test_forward_without_telethon_lookup(env):
    cfg, session, bot, llm, me, service, feed = env
    fwd = {"type": "hidden_user", "date": 1700000000, "sender_user_name": "Mystery"}
    await feed(private_msg("Can you send the invoice?", forward=fwd))
    await asyncio.sleep(2.2)
    s = service.store.get_suggestion(1)
    assert s.source == "forward" and s.chat_id is None
    final = session.of(EditMessageText)[-1]
    assert "Forwarded from Mystery" in final.text
    labels = [b.text for row in final.reply_markup.inline_keyboard for b in row]
    assert not any(label.startswith("📤") for label in labels)  # nowhere to send


async def test_strangers_are_ignored(env):
    cfg, session, bot, llm, me, service, feed = env
    upd = Update.model_validate({"update_id": next(_uid), "message": {
        "message_id": 9, "date": 1, "chat": {"id": 999, "type": "private"},
        "from": {"id": 999, "is_bot": False, "first_name": "X"}, "text": "/start",
        "entities": [{"type": "bot_command", "offset": 0, "length": 6}],
    }})
    await feed(upd)
    assert session.of(SendMessage) == []


async def test_whoami_in_group_and_private(env):
    cfg, session, bot, llm, me, service, feed = env

    def cmd(chat):
        return Update.model_validate({"update_id": next(_uid), "message": {
            "message_id": next(_uid), "date": 1, "chat": chat,
            "from": {"id": 999, "is_bot": False, "first_name": "X"}, "text": "/whoami",
            "entities": [{"type": "bot_command", "offset": 0, "length": 7}],
        }})

    # Works in an unlisted group too, and doesn't trigger the "not in my list" notice.
    await feed(cmd({"id": -100777, "type": "supergroup", "title": "New Group"}))
    reply = session.of(SendMessage)[-1]
    assert reply.chat_id == -100777 and "-100777" in reply.text and "New Group" in reply.text
    assert not any("not in my list" in m.text for m in session.of(SendMessage))

    # In a private chat it answers with the sender's own id (anyone, not only the owner).
    await feed(cmd({"id": 999, "type": "private"}))
    reply = session.of(SendMessage)[-1]
    assert reply.chat_id == 999 and "Your user id: <code>999</code>" in reply.text


async def test_choose_first_then_direct_custom(env):
    cfg, session, bot, llm, me, service, feed = env
    cfg.auto_draft = False
    await feed(group_msg(1, 6, "Ben", "Steven can you check the VPN"))
    assert llm.prompts == []  # no AI call until you choose
    view = session.of(SendMessage)[-1]
    assert "How do you want to reply" in view.text
    labels = [b.text for row in view.reply_markup.inline_keyboard for b in row]
    assert labels == ["💬 Reply", "👍 Acknowledge", "🔎 Gather info", "✍️ Custom"]

    # Replying straight to the notification gives custom drafts in one step.
    s = service.store.get_suggestion(1)
    await feed(private_msg("will check tonight", reply_to_id=s.owner_msg_id))
    assert len(llm.prompts) == 1 and "My intent: will check tonight" in llm.prompts[0][1]
    assert "draft 1" in session.of(EditMessageText)[-1].text


async def test_choose_first_then_mode_button(env):
    cfg, session, bot, llm, me, service, feed = env
    cfg.auto_draft = False
    await feed(group_msg(1, 6, "Ben", "Steven server down"))
    await feed(button("m:1:ack"))
    assert len(llm.prompts) == 1 and "acknowledgement" in llm.prompts[0][1]


async def test_draft_single_group_then_adjust_and_send(env):
    cfg, session, bot, llm, me, service, feed = env
    await feed(group_msg(1, 5, "Ann", "anyone know when the VPN upgrade is?"))
    await feed(private_msg("/draft vpn maintenance tonight 11pm, 15 min downtime"))
    system, user = llm.prompts[-1]
    assert "NEW message" in user and "Target message" not in user
    assert "Ann: anyone know" in user and "11pm" in user
    s = service.store.get_suggestion(1)
    assert s.source == "compose" and s.chat_id == GROUP and s.chat_title == f"Group {GROUP}"
    final = session.of(EditMessageText)[-1]
    assert "New message to" in final.text and "draft 1" in final.text
    labels = [b.text for row in final.reply_markup.inline_keyboard for b in row]
    assert labels == ["📤 Send 1", "📤 Send 2", "📤 Send 3", "🔄 Regenerate"]

    await feed(private_msg("mention it is for NetBird too", reply_to_id=s.owner_msg_id))
    assert "Adjustment: mention it is for NetBird too" in llm.prompts[-1][1]
    assert service.store.get_suggestion(1).mode == "new"

    await feed(button("y:1:1"))
    assert me.sent == [(GROUP, "draft 1", None)]  # a new message, not a reply


async def test_draft_asks_which_group(env):
    cfg, session, bot, llm, me, service, feed = env
    service.bot_cfgs[bot.id].groups.append(-100555)
    await feed(private_msg("/draft hello all"))
    assert llm.prompts == []
    ask = session.of(SendMessage)[-1]
    assert "Which group" in ask.text
    labels = [b.text for row in ask.reply_markup.inline_keyboard for b in row]
    assert labels == [f"Group {GROUP}", "Group -100555", "✖ Cancel"]
    await feed(button("g:1:-100555"))
    assert service.store.get_suggestion(1).chat_id == -100555
    assert len(llm.prompts) == 1
    await feed(button("g:1:-100999"))  # not a listed group: ignored
    assert service.store.get_suggestion(1).chat_id == -100555


async def test_draft_without_text_shows_usage(env):
    cfg, session, bot, llm, me, service, feed = env
    await feed(private_msg("/draft"))
    assert "/draft remind everyone" in session.of(SendMessage)[-1].text
