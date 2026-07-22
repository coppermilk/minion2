# Telethon Premium-Emoji Userbot

A minimal **Telethon** application (a *userbot* -- a real user account over
MTProto, **not** a Bot API bot) that, on startup, posts a proof-of-work message
containing a **premium / custom emoji** to a Telegram chat.

## Why a userbot and not a bot

Premium (custom) emoji can only be **sent** by a user account with Telegram
Premium. The Bot API cannot send them. That is why this uses Telethon with a
user session rather than a bot token.

## How premium emoji are sent

A premium emoji is a normal fallback glyph in the message text (e.g. a phone glyph) plus a
`MessageEntityCustomEmoji` entity that points that glyph's span at the emoji's
`document_id`. `premium_emoji.py` parses the Bot-API-style markup

```
<tg-emoji emoji-id="5334681713316479679">X</tg-emoji>
```

into `(text, entities)`, taking care to measure entity offsets/lengths in
**UTF-16 code units** as Telegram requires.

## Caption under the post (the social bar)

The proof-of-work is sent as **one post** with a **caption line underneath**:
a row of colored premium platform emoji (black TikTok, red YouTube, pink
Instagram, red Pinterest), each **tappable** and linked to its platform.

This is a **signature line in the message text -- not bot buttons.** That is
deliberate: a user account cannot send real inline buttons (bot-only), and
buttons never render premium emoji anyway. As a text caption, a userbot can
post it, and the colors are whatever the premium emoji are (any color, unlike
the three preset button colors bots are limited to).

Each glyph in the caption carries a `MessageEntityCustomEmoji` (its color,
the platform logo) and an overlapping `MessageEntityTextUrl` (its tap target)
on the same span. `build_post_with_bar()` stitches the post and the caption
into one message, shifting the caption's entity offsets past the post text.

Configure the caption in `SOCIAL_BAR` in `main.py` -- one `Social(name,
emoji_id, fallback, url)` per platform. Set each `emoji_id` to that platform's
premium custom-emoji document id (currently they all reuse the one id we have;
swap in the real per-platform ids to get the exact logos), and point each
`url` at your own profile. An entry with `emoji_id=None` falls back to its
plain glyph.

## The bottom button ("plate"): `post_bot.py`

A bottom full-width button under the post (a VIDEO-style call-to-action) is an
**inline keyboard button**, which only a **bot** can attach -- a user account
cannot. So `post_bot.py` logs in with a **bot token** (from @BotFather) and
posts one message that combines both things you wanted:

- **premium emoji inline in the text** (colored bullets), and
- **one full-width bottom button** whose icon is a premium emoji
  (`KeyboardButtonStyle.icon`, from the Bot API 9.4 MTProto layer).

Requirements and notes:

- The bot's **owner must have Telegram Premium** to send premium emoji (in the
  text or on the button), and the bot must be in the target chat.
- If the installed Telethon predates the 9.4 button layer, the button icon
  falls back to a plain glyph in the label (the app warns and still posts).
- The web-page preview is disabled, so no link card covers the post.
- Set `BUTTON_TEXT` in `.env` for a non-ASCII label (e.g. a Russian caption) --
  the `.py` source stays ASCII.

```bash
# in .env: TELEGRAM_BOT_TOKEN=... (plus API_ID / API_HASH)
python post_bot.py
```

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

## Run in Docker (as part of this project)

This runs as a **separate container** in the project's root `docker-compose.yml`
(service `premium-emoji`), built from this folder. It has its own tiny image
(telethon, no torch) and does not ride the shared minion image, but it mounts
the same `${DRIVE_NAS}:/data` volume -- so its session file lives at
`/data/bots/premium-emoji/session.session` and survives restarts and shutdowns.

From the **repository root**:

```bash
cp .env.example .env        # fill in TELEGRAM_API_ID / TELEGRAM_API_HASH (+ DRIVE_NAS)

# 1) First login -- interactive, once. Asks for phone, code, and 2FA password.
docker compose run --rm premium-emoji

# 2) Then run it in the background. Silent login from the saved session.
docker compose up -d --build premium-emoji
```

Logs / stop:

```bash
docker compose logs -f premium-emoji
docker compose stop premium-emoji
```

The session is a file on the shared `/data` mount, independent of the
container's lifecycle -- only deleting that file forces a new login. Set
`TELEGRAM_PASSWORD` in `.env` for a fully non-interactive first login.

## Log in once -- reboots don't ask again

You log in **once**, not every time the machine restarts. After the first login
the auth key is saved and every later start is silent -- a shutdown/reboot does
not wipe it.

The session is saved as a **file** -- `telethon_premium_emoji.session`, right next
to `main.py`. Just run `python main.py`, log in once, and that file persists on
disk across shutdowns. Nothing else to do.

**If your machine's shutdown wipes the working folder**, point the session file
at a path that survives (a mounted/persistent volume) so it isn't lost:

```bash
# in .env -- ".session" is appended for you
TELEGRAM_SESSION_FILE=/data/telethon_premium_emoji
```

The app logs the exact file on startup (`Session store: ...`).

> The `.session` file is full access to the account. It is git-ignored; don't
> share it, and revoke it from Telegram -> Settings -> Devices if it leaks.

<details>
<summary>Alternative: a portable session string instead of a file</summary>

If you'd rather not keep a file at all (e.g. a throwaway container with no
persistent disk), run `python login.py` once. It prints a `TELEGRAM_SESSION=...`
line to paste into `.env`; `main.py` then logs in from that string with no file.

</details>

## Session storage & your 2FA password

**Where the session is saved.** After the first login Telethon stores an *auth
key* (a persistent access key for your account) so later runs don't ask for the
phone/code again. Two options:

| Mode | How to enable | Where it lives |
|------|---------------|----------------|
| **File session** (default) | leave `TELEGRAM_SESSION` unset | `telethon_premium_emoji.session` (SQLite) **next to `main.py`** -- the path is anchored to the script, not your current directory |
| **String session** | set `TELEGRAM_SESSION=<string>` | only in that env var / memory -- nothing written to disk |

The app logs the exact session location on startup (`Session store: ...`).

> Note: **The session file is as sensitive as your password** -- anyone who copies
> it has full access to your account. It is covered by `.gitignore` so it is
> never committed. Don't share it, and revoke it from **Telegram -> Settings ->
> Devices** if it leaks.

**Your 2FA / cloud password.** If your account has two-step verification, the
password is needed **once, at login**, to authorise the session. Telethon uses
it only to authenticate -- **it is never written into the session file.** After
login the saved auth key is what keeps you logged in, not the password.

- Leave `TELEGRAM_PASSWORD` unset -> Telethon prompts for it securely (`getpass`,
  no echo) at first login, only if 2FA is actually enabled.
- Set `TELEGRAM_PASSWORD` in `.env` -> used automatically for a non-interactive
  run (server/cron). `.env` is git-ignored.

To move to another machine without re-entering anything, generate a
`StringSession` once (command in `.env.example`) and pass it via
`TELEGRAM_SESSION` -- then no `.session` file and no password prompt are needed.

## Extending

Import `build_premium_message()` and pass its `.text` / `.entities` to
`client.send_message(chat, text, formatting_entities=entities)` anywhere you
want to send premium emoji.
