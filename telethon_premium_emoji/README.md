# Telethon Premium-Emoji Userbot

A minimal **Telethon** application (a *userbot* — a real user account over
MTProto, **not** a Bot API bot) that, on startup, posts a proof-of-work message
containing a **premium / custom emoji** to a Telegram chat.

## Why a userbot and not a bot

Premium (custom) emoji can only be **sent** by a user account with Telegram
Premium. The Bot API cannot send them. That is why this uses Telethon with a
user session rather than a bot token.

## How premium emoji are sent

A premium emoji is a normal fallback glyph in the message text (e.g. 📱) plus a
`MessageEntityCustomEmoji` entity that points that glyph's span at the emoji's
`document_id`. `premium_emoji.py` parses the Bot-API-style markup

```
<tg-emoji emoji-id="5334681713316479679">📱</tg-emoji>
```

into `(text, entities)`, taking care to measure entity offsets/lengths in
**UTF-16 code units** as Telegram requires.

## Setup

```bash
cd telethon_premium_emoji
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then fill in TELEGRAM_API_ID / TELEGRAM_API_HASH
```

Get `API_ID` / `API_HASH` from <https://my.telegram.org>.

## Run

```bash
python main.py
```

The first run logs you in interactively (phone number, login code, and 2FA
password if you have one) and saves a session. Every run after that logs in
silently. On startup it:

1. Confirms it is a **user** account (aborts if it's a bot).
2. Sends the premium-emoji proof-of-work to the target chat
   (default `-1002431466060`, override with `TARGET_CHAT_ID`).
3. Stays connected so you can extend it with your own handlers.

## Session storage & your 2FA password

**Where the session is saved.** After the first login Telethon stores an *auth
key* (a persistent access key for your account) so later runs don't ask for the
phone/code again. Two options:

| Mode | How to enable | Where it lives |
|------|---------------|----------------|
| **File session** (default) | leave `TELEGRAM_SESSION` unset | `telethon_premium_emoji.session` (SQLite) **next to `main.py`** — the path is anchored to the script, not your current directory |
| **String session** | set `TELEGRAM_SESSION=<string>` | only in that env var / memory — nothing written to disk |

The app logs the exact session location on startup (`Session store: …`).

> ⚠️ **The session file is as sensitive as your password** — anyone who copies
> it has full access to your account. It is covered by `.gitignore` so it is
> never committed. Don't share it, and revoke it from **Telegram → Settings →
> Devices** if it leaks.

**Your 2FA / cloud password.** If your account has two-step verification, the
password is needed **once, at login**, to authorise the session. Telethon uses
it only to authenticate — **it is never written into the session file.** After
login the saved auth key is what keeps you logged in, not the password.

- Leave `TELEGRAM_PASSWORD` unset → Telethon prompts for it securely (`getpass`,
  no echo) at first login, only if 2FA is actually enabled.
- Set `TELEGRAM_PASSWORD` in `.env` → used automatically for a non-interactive
  run (server/cron). `.env` is git-ignored.

To move to another machine without re-entering anything, generate a
`StringSession` once (command in `.env.example`) and pass it via
`TELEGRAM_SESSION` — then no `.session` file and no password prompt are needed.

## Extending

Import `build_premium_message()` and pass its `.text` / `.entities` to
`client.send_message(chat, text, formatting_entities=entities)` anywhere you
want to send premium emoji.
