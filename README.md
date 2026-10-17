# my-tg-digital-clone

A Telegram reply assistant. It watches the groups you choose, and when someone
mentions you it privately sends you draft replies written by your own
self-hosted model (Ollama / Gemma). You can also forward it any message to get
drafts. Optionally, it can send the chosen draft **as you**.

Nothing is ever sent without you tapping a button.

## What it does

- **Group mentions**: triggers when someone writes your `@username`, mentions you,
  replies to one of your messages, or says one of your names/nicknames.
  It waits a few seconds so multi-message bursts are read together, and skips the
  suggestion if you answer yourself first.
- **Forwards**: forward any message to the bot. With "Send as me" enabled it finds
  the original chat (even if the sender hides forwards), reads the recent
  conversation for context, and replies there as a threaded reply.
- **Modes**: 💬 Reply · 👍 Acknowledge (restates the actual request, never just
  "noted") · 🔎 Gather info (asks the questions an experienced engineer would ask) ·
  ✍️ Custom (tell it what you want to say) · 🔄 Regenerate.
- **Send as me** (optional): tap 📤, then confirm. Or reply to a suggestion with
  `/send your own text` to send exactly that.
- **Several bots, one container**: add `BOT_2_TOKEN`, `BOT_3_TOKEN`… with their own groups.
- Unlisted groups are ignored, and the bot tells you their chat id so you can add them.

## Setup

### 1. Create the bot

1. In [@BotFather](https://t.me/BotFather): `/newbot`, keep the token.
2. **Turn privacy mode off**: `/setprivacy` → choose the bot → **Disable**.
   Without this the bot only sees messages that mention the bot itself.
3. Add the bot to your group(s). If it was already in a group before step 2,
   remove it and add it again.
4. Open a private chat with the bot and press **Start** (otherwise it can't message you).

### 2. On the server

```bash
mkdir tg-digital-clone && cd tg-digital-clone
curl -O https://raw.githubusercontent.com/stevenfoong/my-tg-digital-clone/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/stevenfoong/my-tg-digital-clone/main/.env.example
curl -o prompts.example.yaml https://raw.githubusercontent.com/stevenfoong/my-tg-digital-clone/main/prompts.example.yaml

mkdir -p data && cp prompts.example.yaml data/prompts.yaml
sudo chown -R 10001:10001 data        # the container runs as an unprivileged user
chmod 600 .env

nano .env                 # tokens, your ids, model URL, Cloudflare Access token
nano data/prompts.yaml    # who you are and how you write

docker compose up -d
docker compose logs -f
```

Don't know a group's id or your own user id? Start the bot and send `/whoami` in the
group, or in a private chat with the bot.

If `docker pull` says *unauthorized*, the image is private: either make the
package public (GitHub → your profile → Packages → this package → Package
settings → Change visibility), or run `docker login ghcr.io` with a token that
has `read:packages`.

### 3. Optional: Send as me

1. Get `api_id` and `api_hash` at <https://my.telegram.org> → API development tools.
2. In `.env`: `TELETHON_ENABLED=true`, `TG_API_ID=…`, `TG_API_HASH=…`.
3. Log in once (asks for your phone, the login code and your 2FA password):
   ```bash
   docker compose run --rm bot python -m app.login
   docker compose up -d --force-recreate
   ```

The session file `data/me.session` **is your Telegram account**. Anyone who has it
can use your account without a password. Keep it out of git and backups you share.
If it ever leaks: Telegram → Settings → Devices → terminate that session.

## Using it

- Mentioned in a group → you get a private message asking how to reply: pick 💬 Reply,
  👍 Acknowledge or 🔎 Gather info, or just reply to that message with what you want to
  say for custom drafts. The AI is only called after you choose (set `AUTO_DRAFT=true`
  to get normal drafts immediately instead). Tap a draft to copy it.
- Buttons switch modes or regenerate. **Custom** asks what you want to say.
  Replying to a suggestion message with text does the same.
- 📤 **Send N** → ✅ **Confirm** posts it as you, as a reply to the original message.
- `/status` shows what's running, `/reload` re-reads `data/prompts.yaml`.
- `/whoami` in a group replies with the group's chat id (works even before the group
  is listed in `.env`); in a private chat it replies with your user id (for `OWNER_ID`).

## Updating

Every push to `main` builds a new multi-arch image (amd64 + arm64) at
`ghcr.io/stevenfoong/my-tg-digital-clone:latest`. On the server:

```bash
docker compose pull && docker compose up -d
```

Or enable the optional Watchtower service in `docker-compose.yml` to do this automatically.

## Configuration reference

See [.env.example](.env.example). Highlights:

| Variable | Meaning |
| --- | --- |
| `BOT_n_TOKEN`, `BOT_n_GROUPS` | One pair per bot. Groups are comma-separated chat ids. |
| `OWNER_ID`, `OWNER_USERNAME`, `OWNER_ALIASES` | Who "me" is, and which names count as a mention. |
| `LLM_BASE_URL`, `LLM_MODEL` | Your Ollama server and model (default `gemma4:e4b`). |
| `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET` | Cloudflare Access service token headers. |
| `LLM_EXTRA_HEADERS` | Any other headers as JSON. |
| `LLM_API` | `ollama` (default) or `openai` for any OpenAI-compatible server. |
| `TELETHON_ENABLED`, `TG_API_ID`, `TG_API_HASH` | Send as me. |
| `CONTEXT_MESSAGES`, `DEBOUNCE_SECONDS`, `DRAFT_COUNT` | Tuning. |
| `LEAVE_UNLISTED_GROUPS` | `true` makes bots leave groups that aren't listed. |

## Privacy and security

- Secrets and personal details live only in `.env` and `data/` on your server.
  `.gitignore` and `.dockerignore` exclude them, and every push runs a secret scan.
- Group messages go only to your own model server. The bot keeps a rolling buffer
  of recent messages per watched group in `data/assistant.db`; old suggestions are pruned.
- Everyone in a group can see the bot is a member. In work groups, tell people what it does.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```
