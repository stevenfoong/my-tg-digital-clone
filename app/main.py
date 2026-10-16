"""Entry point: runs every configured bot in one process."""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher

from .config import ConfigError, load_config
from .db import Store
from .detect import MentionDetector
from .handlers import build_router
from .llm import LLMClient
from .me_client import MeClient
from .prompts import Prompts
from .service import Service

log = logging.getLogger("app")


async def _prune_loop(store: Store) -> None:
    while True:
        await asyncio.sleep(6 * 3600)
        try:
            store.prune()
        except Exception:
            log.exception("Prune failed")


async def run() -> None:
    try:
        cfg = load_config()
    except ConfigError as exc:
        logging.basicConfig(level=logging.INFO)
        log.error("Configuration problem: %s", exc)
        sys.exit(2)

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every request URL at INFO; keep the logs quiet and token-free.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    store = Store(cfg.data_dir / "assistant.db")
    prompts = Prompts(cfg.prompts_file)
    llm = LLMClient(cfg.llm)
    me = MeClient(cfg.telethon)
    try:
        await me.start()
    except Exception:
        log.exception("Telethon failed to start; 'Send as me' is disabled")

    service = Service(cfg, store, llm, prompts, me)
    detector = MentionDetector(cfg.owner_id, cfg.owner_username, cfg.owner_aliases)

    bots: list[Bot] = []
    for bcfg in cfg.bots:
        bot = Bot(bcfg.token)
        try:
            info = await bot.get_me()
        except Exception as exc:
            log.error("BOT_%s_TOKEN was rejected by Telegram (%s); skipping it", bcfg.index, exc)
            await bot.session.close()
            continue
        if not info.can_read_all_group_messages:
            log.warning(
                "@%s has privacy mode ON, so it only sees messages that mention the bot. "
                "In @BotFather: /setprivacy -> Disable, then remove and re-add it to your groups.",
                info.username,
            )
        if not bcfg.groups:
            log.warning("@%s has no groups in BOT_%s_GROUPS; it will only handle forwards", info.username, bcfg.index)
        service.register_bot(bot, bcfg)
        bots.append(bot)
        log.info("Bot @%s ready, watching %d group(s)", info.username, len(bcfg.groups))

    if not bots:
        log.error("No working bots. Check your BOT_n_TOKEN values.")
        sys.exit(2)

    dp = Dispatcher()
    dp.include_router(build_router(service, detector))
    pruner = asyncio.create_task(_prune_loop(store))
    try:
        await dp.start_polling(
            *bots, allowed_updates=["message", "callback_query", "my_chat_member"]
        )
    finally:
        pruner.cancel()
        await llm.close()
        await me.stop()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
