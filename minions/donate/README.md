# Donate (Telethon userbot)

A **Telethon** userbot -- a real user account over MTProto, **not** a Bot API
bot -- that listens for the `/donate` command and renders a formatted donation
**shout-out** with **premium / custom emoji** into a chat.

It is a **separate app from the aggregator**: a Telethon session is one
account, so a bot that runs on a *different* account needs its own session and
its own credentials. The two share only `premium_emoji.py` (the pure entity
builder), which lives in the aggregator package.

## Why a userbot and not a bot

Premium (custom) emoji can only be **sent** by a user account with Telegram
Premium; the Bot API cannot. So this uses Telethon with a **user session**, not
a bot token.

## The command

```
/donate <name> <amount> <message>
```

`name` and `amount` are single tokens; everything after is the message. The
rendered post is: a leading premium emoji + header (`<LABEL> . <name> .
<amount><currency>`), the quoted `message`, a randomly chosen reaction line,
then a footer of links. Malformed input gets a short usage reply instead.

The donor name and message are sent as **plain text with explicit MTProto
entities** (no HTML parse mode), so there is nothing to escape or inject.

## Files

| File | What it is |
|------|-----------|
| `main.py` | the bot itself (`python -m minions.donate.main`) |
| `donate_constants.json` | editable texts + the leading premium emoji id (UTF-8) |
| `login.py` | log in once and write the session file (`python -m minions.donate.login`) |

`premium_emoji.py` (the `RichText` builder) is imported from the aggregator
package -- it is a shared, account-agnostic utility.

## Configuration

Two sources, no overlap: the **env** carries only the deploy knobs; every text
and the emoji live in **`donate_constants.json`**.

Env (all `DONATE_`-prefixed so they never collide with the aggregator's):

| Var | Meaning |
|-----|---------|
| `DONATE_API_ID`, `DONATE_API_HASH` | the account's API credentials (my.telegram.org) |
| `DONATE_PASSWORD` | optional 2FA/cloud password (else prompted once) |
| `DONATE_CHAT_ID` | where to render; unset = reply in the command's chat |
| `DONATE_SESSION_FILE` | session-file path override |

The session defaults to `<DRIVE>/bots/donate/telethon` (the `/data` mount in
the container), else next to this package.

## Editing the look

`donate_constants.json` holds `donate_emoji` (a premium `{id, fallback}`
entry -- swap in a plain glyph string for a non-premium emoji), the
`donate_header` / `donate_quote` / `donate_reactions` templates (which accept
`{name}`, `{amount}` and `{message}`), the `donate_separator`, the
`donate_arrow`, and `donate_links` (a `{label, url}` per footer row -- an empty
url renders the link text as plain text until you fill it in).
