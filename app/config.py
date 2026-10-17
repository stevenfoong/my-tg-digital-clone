"""Configuration loaded from environment variables (.env on the server).

Nothing personal or secret lives in the repo: every value here comes from the
environment at runtime.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    pass


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _bool(name: str, default: bool = False) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    value = _env(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {value!r}") from exc


def _int_list(raw: str | None, name: str) -> list[int]:
    if not raw:
        return []
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError as exc:
            raise ConfigError(f"{name} contains a non-numeric chat id: {part!r}") from exc
    return out


def _str_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


@dataclass
class BotConfig:
    index: int
    token: str
    groups: list[int]

    @property
    def bot_id(self) -> int:
        # The numeric bot id is the part of the token before the colon.
        return int(self.token.split(":", 1)[0])


@dataclass
class LLMConfig:
    api: str  # "ollama" or "openai"
    base_url: str
    model: str
    timeout: int
    headers: dict[str, str]
    think: bool | None
    temperature: float


@dataclass
class TelethonConfig:
    enabled: bool
    api_id: int | None
    api_hash: str | None
    session_path: str


@dataclass
class Config:
    bots: list[BotConfig]
    owner_id: int
    owner_username: str | None
    owner_aliases: list[str]
    llm: LLMConfig
    telethon: TelethonConfig
    data_dir: Path
    prompts_file: Path | None
    context_messages: int
    debounce_seconds: int
    draft_count: int
    leave_unlisted_groups: bool
    buffer_limit: int
    log_level: str
    all_groups: set[int] = field(default_factory=set)


def _load_bots() -> list[BotConfig]:
    bots: list[BotConfig] = []
    pattern = re.compile(r"^BOT_(\d+)_TOKEN$")
    indexes = sorted(
        int(m.group(1)) for key in os.environ if (m := pattern.match(key)) and _env(key)
    )
    for idx in indexes:
        token = _env(f"BOT_{idx}_TOKEN")
        if not token or ":" not in token:
            raise ConfigError(f"BOT_{idx}_TOKEN does not look like a Telegram bot token")
        groups = _int_list(_env(f"BOT_{idx}_GROUPS"), f"BOT_{idx}_GROUPS")
        bots.append(BotConfig(index=idx, token=token, groups=groups))

    # Single-bot shorthand: BOT_TOKEN + GROUP_IDS
    if not bots and _env("BOT_TOKEN"):
        bots.append(
            BotConfig(
                index=1,
                token=_env("BOT_TOKEN"),  # type: ignore[arg-type]
                groups=_int_list(_env("GROUP_IDS"), "GROUP_IDS"),
            )
        )
    if not bots:
        raise ConfigError("No bots configured. Set BOT_1_TOKEN (and BOT_1_GROUPS).")
    return bots


def _load_llm_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    raw = _env("LLM_EXTRA_HEADERS")
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError("LLM_EXTRA_HEADERS must be a JSON object") from exc
        if not isinstance(parsed, dict):
            raise ConfigError("LLM_EXTRA_HEADERS must be a JSON object")
        headers.update({str(k): str(v) for k, v in parsed.items()})
    cf_id = _env("CF_ACCESS_CLIENT_ID")
    cf_secret = _env("CF_ACCESS_CLIENT_SECRET")
    if cf_id and cf_secret:
        headers["CF-Access-Client-Id"] = cf_id
        headers["CF-Access-Client-Secret"] = cf_secret
    elif cf_id or cf_secret:
        raise ConfigError("Set both CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET, or neither")
    api_key = _env("LLM_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def load_config() -> Config:
    bots = _load_bots()

    owner_raw = _env("OWNER_ID")
    if not owner_raw:
        raise ConfigError("OWNER_ID (your numeric Telegram user id) is required")
    try:
        owner_id = int(owner_raw)
    except ValueError as exc:
        raise ConfigError("OWNER_ID must be numeric") from exc

    username = _env("OWNER_USERNAME")
    if username:
        username = username.lstrip("@")

    api = (_env("LLM_API", "ollama") or "ollama").lower()
    if api not in ("ollama", "openai"):
        raise ConfigError("LLM_API must be 'ollama' or 'openai'")
    base_url = _env("LLM_BASE_URL")
    if not base_url:
        raise ConfigError("LLM_BASE_URL is required (e.g. https://gemma.example.com)")

    think_raw = _env("LLM_THINK")
    think = None if think_raw is None else think_raw.lower() in ("1", "true", "yes", "on")

    try:
        temperature = float(_env("LLM_TEMPERATURE", "0.6") or "0.6")
    except ValueError as exc:
        raise ConfigError("LLM_TEMPERATURE must be a number") from exc

    llm = LLMConfig(
        api=api,
        base_url=base_url.rstrip("/"),
        model=_env("LLM_MODEL", "gemma4:e4b") or "gemma4:e4b",
        timeout=_int("LLM_TIMEOUT", 120),
        headers=_load_llm_headers(),
        think=think,
        temperature=temperature,
    )

    data_dir = Path(_env("DATA_DIR", "/data") or "/data")

    tele_enabled = _bool("TELETHON_ENABLED", False)
    api_id_raw = _env("TG_API_ID")
    tele = TelethonConfig(
        enabled=tele_enabled,
        api_id=int(api_id_raw) if api_id_raw else None,
        api_hash=_env("TG_API_HASH"),
        session_path=_env("TELETHON_SESSION", str(data_dir / "me")) or str(data_dir / "me"),
    )
    if tele.enabled and (not tele.api_id or not tele.api_hash):
        raise ConfigError("TELETHON_ENABLED=true needs TG_API_ID and TG_API_HASH")

    prompts_raw = _env("PROMPTS_FILE")
    # Always point at a path, even if the file doesn't exist yet, so a file
    # created later is picked up by /reload without restarting.
    prompts_file = Path(prompts_raw) if prompts_raw else data_dir / "prompts.yaml"

    cfg = Config(
        bots=bots,
        owner_id=owner_id,
        owner_username=username,
        owner_aliases=_str_list(_env("OWNER_ALIASES")),
        llm=llm,
        telethon=tele,
        data_dir=data_dir,
        prompts_file=prompts_file,
        context_messages=_int("CONTEXT_MESSAGES", 30),
        debounce_seconds=_int("DEBOUNCE_SECONDS", 5),
        draft_count=max(1, min(5, _int("DRAFT_COUNT", 3))),
        leave_unlisted_groups=_bool("LEAVE_UNLISTED_GROUPS", False),
        buffer_limit=_int("BUFFER_LIMIT", 200),
        log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
    )
    cfg.all_groups = {g for b in bots for g in b.groups}
    return cfg
