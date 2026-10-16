"""One-time interactive login for "Send as me".

Run inside the container:  docker compose run --rm bot python -m app.login
It asks for your phone number, the code Telegram sends you, and your 2FA
password (if set), then saves the session file in the data volume.
"""
from __future__ import annotations

import asyncio
import os
import sys

from telethon import TelegramClient


async def main() -> None:
    api_id = os.environ.get("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH")
    data_dir = os.environ.get("DATA_DIR", "/data")
    session = os.environ.get("TELETHON_SESSION") or os.path.join(data_dir, "me")
    if not api_id or not api_hash:
        sys.exit("Set TG_API_ID and TG_API_HASH in .env first (from https://my.telegram.org).")

    os.makedirs(os.path.dirname(os.path.abspath(session)) or ".", exist_ok=True)
    client = TelegramClient(session, int(api_id), api_hash)
    await client.start()  # prompts for phone, code and 2FA password
    me = await client.get_me()
    await client.disconnect()

    session_file = session if session.endswith(".session") else session + ".session"
    try:
        os.chmod(session_file, 0o600)
    except OSError:
        pass
    print(f"\nLogged in as {me.first_name} (id {me.id}). Session saved to {session_file}.")
    print("Keep this file private: anyone who has it can use your Telegram account.")


if __name__ == "__main__":
    asyncio.run(main())
