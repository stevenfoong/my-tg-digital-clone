import json

import httpx
import pytest

from app.config import ConfigError, LLMConfig, load_config
from app.db import StoredMessage, Store
from app.detect import Entity, MentionDetector, entity_text
from app.llm import LLMClient, LLMError, parse_drafts
from app.prompts import Prompts

OWNER = 111


# ---------------- mention detection ----------------

@pytest.fixture
def det():
    return MentionDetector(OWNER, "@Steven_F", ["Steven", "史蒂文", "boss"])


def test_reply_to_owner(det):
    assert det.reason("ok?", [], reply_to_user_id=OWNER, sender_id=5) == "replied to your message"


def test_username_mention_with_emoji_offsets(det):
    text = "🔥🔥 @steven_f can you check"
    off = len("🔥🔥 ".encode("utf-16-le")) // 2
    ents = [Entity("mention", off, len("@steven_f"))]
    assert entity_text(text, off, len("@steven_f")) == "@steven_f"
    assert det.reason(text, ents, None, 5) == "mentioned @steven_f"


def test_other_username_ignored(det):
    text = "@someoneelse hi"
    assert det.reason(text, [Entity("mention", 0, 12)], None, 5) is None


def test_text_mention(det):
    assert det.reason("Steve pls", [Entity("text_mention", 0, 5, user_id=OWNER)], None, 5) == "mentioned you"


def test_alias_word_boundary(det):
    assert det.reason("hi Steven, server down", [], None, 5) == "said your name"
    assert det.reason("Stevenson called", [], None, 5) is None
    assert det.reason("the BOSS said so", [], None, 5) == "said your name"


def test_cjk_alias(det):
    assert det.reason("史蒂文你好", [], None, 5) == "said your name"


def test_owner_own_message_never_triggers(det):
    assert det.reason("Steven here", [], OWNER, OWNER) is None


# ---------------- draft parsing ----------------

def test_parse_json_object():
    assert parse_drafts('{"drafts": ["a", "b", "c"]}', 3) == ["a", "b", "c"]


def test_parse_fenced_and_extra_text():
    raw = 'Sure!\n```json\n{"drafts": ["one", "two"]}\n```'
    assert parse_drafts(raw, 3) == ["one", "two"]


def test_parse_list_of_objects():
    assert parse_drafts('[{"text": "x"}, {"reply": "y"}]', 3) == ["x", "y"]


def test_parse_numbered_fallback():
    assert parse_drafts("1. hello\n2) there\n- third", 2) == ["hello", "there"]


def test_parse_caps_count():
    assert parse_drafts('{"drafts": ["a","b","c","d"]}', 2) == ["a", "b"]


def test_parse_empty_raises():
    with pytest.raises(LLMError):
        parse_drafts("   ", 3)


# ---------------- config ----------------

def _base_env(monkeypatch, tmp_path):
    for key in list(__import__("os").environ):
        if key.startswith(("BOT_", "CF_ACCESS", "LLM_", "TG_", "TELETHON", "OWNER", "GROUP_IDS")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OWNER_ID", str(OWNER))
    monkeypatch.setenv("LLM_BASE_URL", "https://gemma.example.com/")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))


def test_multi_bot_config(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("BOT_1_TOKEN", "123:aaa")
    monkeypatch.setenv("BOT_1_GROUPS", "-1001, -1002")
    monkeypatch.setenv("BOT_2_TOKEN", "456:bbb")
    monkeypatch.setenv("BOT_2_GROUPS", "-1003")
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "id.access")
    monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", "sec")
    cfg = load_config()
    assert [b.bot_id for b in cfg.bots] == [123, 456]
    assert cfg.bots[0].groups == [-1001, -1002]
    assert cfg.all_groups == {-1001, -1002, -1003}
    assert cfg.llm.base_url == "https://gemma.example.com"
    assert cfg.llm.headers["CF-Access-Client-Id"] == "id.access"
    assert cfg.llm.model == "gemma4:e4b"


def test_config_requires_both_cf_values(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("BOT_1_TOKEN", "123:aaa")
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "only-id")
    with pytest.raises(ConfigError):
        load_config()


def test_config_telethon_needs_api(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("BOT_1_TOKEN", "123:aaa")
    monkeypatch.setenv("TELETHON_ENABLED", "true")
    with pytest.raises(ConfigError):
        load_config()


# ---------------- store ----------------

def test_buffer_trims_and_orders(tmp_path):
    store = Store(tmp_path / "t.db")
    for i in range(1, 8):
        store.add_message(StoredMessage(-1, i, 5, "Ann", f"m{i}", 1000 + i, None, i == 7), keep=5)
    msgs = store.recent_messages(-1, 10)
    assert [m.text for m in msgs] == ["m3", "m4", "m5", "m6", "m7"]
    assert store.owner_spoke_after(-1, 6)
    assert not store.owner_spoke_after(-1, 7)


def test_suggestion_roundtrip(tmp_path):
    store = Store(tmp_path / "t.db")
    sid = store.create_suggestion(
        bot_id=1, source="group", chat_id=-100, chat_title="Ops", target_msg_id=9,
        target_ts=1, target_sender_id=5, target_sender_name="Ann", target_text="server down",
        context="", mode="reply", reason="said your name", drafts=["a"],
    )
    store.update_suggestion(sid, owner_msg_id=77, drafts=["x", "y"])
    s = store.suggestion_by_owner_msg(1, 77)
    assert s and s.id == sid and s.drafts == ["x", "y"] and s.reason == "said your name"
    store.add_pending_custom(1, 88, sid)
    assert store.pop_pending_custom(1, 88) == sid
    assert store.pop_pending_custom(1, 88) is None


# ---------------- prompts ----------------

def test_prompts_override_and_build(tmp_path):
    f = tmp_path / "prompts.yaml"
    f.write_text("about_me: I run IT for a restaurant group.\nunknown_key: x\n", encoding="utf-8")
    p = Prompts(f)
    system, user = p.build("gather", "Ann: hi", "Ann", "AWS server down", "Ops", None, 3)
    assert "restaurant group" in system and "exactly 3" in system
    assert "AWS server down" in user and "competent" in user


def test_prompts_bad_file_falls_back(tmp_path):
    f = tmp_path / "prompts.yaml"
    f.write_text("- just a list\n", encoding="utf-8")
    p = Prompts(f)
    assert p.values["about_me"].startswith("I am")


# ---------------- LLM client against a fake server ----------------

def _client(handler, api="ollama"):
    cfg = LLMConfig(api=api, base_url="https://llm.test", model="gemma4:e4b", timeout=5,
                    headers={"CF-Access-Client-Id": "i", "CF-Access-Client-Secret": "s"},
                    think=False, temperature=0.5)
    c = LLMClient(cfg)
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=cfg.headers)
    return c


async def test_ollama_request_shape():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": '{"drafts":["Got it"]}'}})

    c = _client(handler)
    drafts = await c.draft("sys", "user", 3)
    assert drafts == ["Got it"]
    assert seen["url"] == "https://llm.test/api/chat"
    assert seen["headers"]["CF-Access-Client-Id"] == "i"
    assert seen["body"]["format"] == "json" and seen["body"]["think"] is False
    assert seen["body"]["stream"] is False


async def test_openai_mode():
    def handler(request: httpx.Request):
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(200, json={"choices": [{"message": {"content": '["a","b"]'}}]})

    assert await _client(handler, api="openai").draft("s", "u", 3) == ["a", "b"]


async def test_cloudflare_login_page_is_reported():
    def handler(request):
        return httpx.Response(200, text="<html>Sign in</html>", headers={"content-type": "text/html"})

    with pytest.raises(LLMError, match="Cloudflare"):
        await _client(handler).draft("s", "u", 3)


async def test_forbidden_is_reported():
    def handler(request):
        return httpx.Response(403, text="forbidden")

    with pytest.raises(LLMError, match="service token"):
        await _client(handler).draft("s", "u", 3)


def test_prompts_file_created_after_start_is_picked_up_by_reload(tmp_path):
    f = tmp_path / "prompts.yaml"
    p = Prompts(f)
    assert p.status.startswith("defaults")
    f.write_text("style: |\n  Very formal.\nfoo: bar\n", encoding="utf-8")
    status = p.reload()
    assert status.startswith("loaded: style") and "foo" in status
    assert p.values["style"] == "Very formal."
    f.write_text("style: [unclosed\n", encoding="utf-8")
    assert p.reload().startswith("ERROR")


def test_config_defaults_prompts_path(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("BOT_1_TOKEN", "123:aaa")
    monkeypatch.delenv("PROMPTS_FILE", raising=False)
    assert load_config().prompts_file == tmp_path / "prompts.yaml"
