# Aggregator (Telethon userbot)

A **Telethon** userbot -- a real user account over MTProto, **not** a Bot API
bot -- that aggregates one Short's links across platforms and posts the
collected message (with **premium / custom emoji**) to one or more chats.

It listens to a **source chat** where a bot (IFTTT/Zapier/etc.) drops one JSON
object per video per platform:

```json
{"platform": "youtube", "caption": "...", "link": "https://...",
 "thumnailUrl": "https://...jpg", "duration": "0:0:16"}
```

Messages whose captions match ~90% are treated as the same video. Once every
expected platform has arrived (or a timeout elapses), **one** message collecting
each platform's link is posted to the **target chat(s)**.

## Why a userbot and not a bot

Premium (custom) emoji can only be **sent** by a user account with Telegram
Premium; the Bot API cannot. So this uses Telethon with a **user session**, not
a bot token. `premium_emoji.py` turns Bot-API-style `<tg-emoji emoji-id="...">`
markup into Telethon `MessageEntityCustomEmoji` entities (measured in UTF-16
code units, as Telegram requires).

## Files

| File | What it is |
|------|-----------|
| `main.py` | the aggregator itself (`python -m minions.aggregator.main`) |
| `premium_emoji.py` | premium-emoji entity builder (`RichText`, `build_post_with_bar`, ...) |
| `aggregator_constants.json` | editable post texts + premium emoji ids (UTF-8) |
| `login.py` | log in once and write the session file (`python -m minions.aggregator.login`) |
| `dump_emoji_ids.py` | dev helper: print the id of every premium emoji you send |

## Configuration

Two sources, no overlap: the **env** carries only the deploy knobs (credentials
and the chats); **`aggregator_constants.json`** carries all behaviour.

**env** (credentials + chats + paths):

| Env | Meaning |
|-----|---------|
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | credentials (from <https://my.telegram.org>) |
| `TELEGRAM_PASSWORD` | optional 2FA/cloud password |
| `SOURCE_CHAT_ID` | the chat the per-platform JSON arrives in (monitoring) |
| `TARGET_CHAT_ID` | target chat(s) -- **comma-separated** to post to several |
| `TELEGRAM_SESSION_FILE` | session-file base path (`.session` appended) |
| `AGGREGATOR_STATE_DIR` | where in-flight state is persisted |

**`aggregator_constants.json`** (behaviour + content):

| Key | Meaning |
|-----|---------|
| `platforms` | expected platforms, in priority order (comma-separated) |
| `title_match` | caption similarity to treat two messages as the same video |
| `timeout_sec` | how long to wait for the rest before posting a partial |
| `backfill` | recent source messages scanned at startup |
| `max_duration_sec` | a video at/above this many seconds is dropped (not a Short) |
| `fields`, `action_value`, `author`, `announce`, `love`, `ps`, `arrow_down`, `view_label`, `rows`, `platform_emoji` | incoming field names + the post's texts and premium emoji |

## Run (without Docker)

```bash
pip install -e '.[tg]'            # from the repo root
cp .env.example .env              # fill in TELEGRAM_API_ID / TELEGRAM_API_HASH
python -m minions.aggregator.main
```

The first run logs you in interactively (phone, code, 2FA if enabled) and writes
`telethon.session` next to this package. Every run after that is silent. That
file is **git-ignored**, so a session kept in the checkout survives a repo
re-sync (`deploy/nas-update.sh`) -- exactly like `.env`.

## Run in Docker (this project's shared image)

The aggregator rides the **one shared image** (`telethon` is baked in via the
`tg` extra) -- there is no separate image to build. Compose (`aggregator`
service) mounts `${DRIVE_NAS}:/data`, sets
`TELEGRAM_SESSION_FILE=/data/bots/aggregator/session`, and reads `.env`.

From the **repository root**:

```bash
cp .env.example .env              # TELEGRAM_API_ID / TELEGRAM_API_HASH (+ DRIVE_NAS)
docker compose run --rm aggregator    # 1) first login, interactive, once
docker compose up -d aggregator       # 2) silent from the saved session
docker compose logs -f aggregator
```

## Log in once -- reboots don't ask again

After the first login the auth key is saved in the session **file**; later
starts are silent across reboots and shutdowns because the file persists (on
`/data` in Docker, or in the checkout when run bare).

**Generate the session on another machine (e.g. Windows) and point at it.**
Run `python -m minions.aggregator.login` there once; it writes and locates the
`.session` file. Copy that file to where the aggregator runs (for Docker,
`/data/bots/aggregator/session.session`) -- **no rebuild**. See
`deploy/windows/README.md`.

> The `.session` file is full account access. It is git-ignored; don't share it,
> and revoke it from **Telegram -> Settings -> Devices** if it leaks.
