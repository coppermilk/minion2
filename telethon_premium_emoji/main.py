"""Telethon user-account app that posts a premium-emoji proof-of-work on start.

This is a *userbot* — it logs in as a real Telegram account over MTProto, not
through the Bot API.  That distinction is the whole point: only a user account
(with Telegram Premium) may send premium / custom emoji.  A Bot API bot cannot,
so this deliberately uses Telethon and a user session.

On startup it sends one message containing a premium emoji to the configured
chat, confirming the account is authorised and premium-emoji delivery works,
then it stays connected so you can extend it with handlers.

Configuration (env vars, or a local .env file):

    TELEGRAM_API_ID     from https://my.telegram.org
    TELEGRAM_API_HASH   from https://my.telegram.org
    TELEGRAM_SESSION    optional StringSession; if unset a file session
                        `telethon_premium_emoji.session` is used
    TARGET_CHAT_ID      override the default proof-of-work chat
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession

from premium_emoji import build_premium_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("premium-emoji")

# Chat the proof-of-work message goes to (the -100… supergroup id from the task).
DEFAULT_TARGET_CHAT_ID = -1002431466060

# Proof-of-work payload. The example premium emoji from the task, plus a line
# of context so it reads as a deliberate startup ping rather than noise.
PROOF_OF_WORK_MARKUP = (
    '<tg-emoji emoji-id="5334681713316479679">📱</tg-emoji> '
    "Telethon userbot online — premium emoji proof-of-work."
)


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so a local file can supply credentials.

    Kept dependency-free on purpose; real values still come from the process
    environment when both are present (the environment wins).
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


# Where the file session lives when TELEGRAM_SESSION is not set. Anchored next
# to this script (not the current working directory) so the same `.session`
# file is reused no matter where you launch from. Telethon appends `.session`,
# so the file on disk is `telethon_premium_emoji.session`.
SESSION_PATH = Path(__file__).with_name("telethon_premium_emoji")


def _build_client() -> tuple[TelegramClient, str]:
    """Build the client and return it with a human description of the session."""
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit(
            "Set TELEGRAM_API_ID and TELEGRAM_API_HASH "
            "(get them from https://my.telegram.org)."
        )

    session_str = os.environ.get("TELEGRAM_SESSION")
    if session_str:
        # StringSession keeps the auth key only in memory / in the env var;
        # nothing is written to disk.
        session: StringSession | str = StringSession(session_str)
        where = "in-memory StringSession (from TELEGRAM_SESSION)"
    else:
        session = str(SESSION_PATH)
        where = f"{SESSION_PATH}.session"
    return TelegramClient(session, int(api_id), api_hash), where


async def send_proof_of_work(client: TelegramClient, chat_id: int) -> None:
    """Send the premium-emoji proof-of-work message to ``chat_id``."""
    message = build_premium_message(PROOF_OF_WORK_MARKUP)
    await client.send_message(chat_id, message.text, formatting_entities=message.entities)
    log.info(
        "Sent proof-of-work to %s with %d premium emoji entit%s.",
        chat_id,
        len(message.entities),
        "y" if len(message.entities) == 1 else "ies",
    )


async def main() -> None:
    _load_dotenv(Path(__file__).with_name(".env"))
    chat_id = int(os.environ.get("TARGET_CHAT_ID", DEFAULT_TARGET_CHAT_ID))

    client, session_where = _build_client()
    log.info("Session store: %s", session_where)

    # First run performs the login: phone number, the code Telegram sends, and —
    # if your account has 2FA (a cloud password) — that password too. Telethon
    # uses the password ONLY to authenticate; it is never written to the session.
    # After a successful login the auth key is saved and later runs are silent.
    #
    # TELEGRAM_PASSWORD lets you supply the 2FA password non-interactively (e.g.
    # for a server/cron run). If it is unset, Telethon prompts for it securely
    # (getpass, no echo) only when the account actually has 2FA enabled.
    start_kwargs: dict[str, object] = {}
    password = os.environ.get("TELEGRAM_PASSWORD")
    if password:
        start_kwargs["password"] = password
    await client.start(**start_kwargs)

    me = await client.get_me()
    if me.bot:
        raise SystemExit(
            "Logged in as a BOT account — premium emoji require a user account. "
            "Use a real user session, not a bot token."
        )
    if not getattr(me, "premium", False):
        log.warning(
            "Account @%s is not Telegram Premium; the custom emoji may render "
            "as its fallback glyph for you but will still send.",
            me.username or me.id,
        )

    log.info("Logged in as @%s (id=%s).", me.username or "-", me.id)
    await send_proof_of_work(client, chat_id)

    log.info("Proof-of-work done. Staying connected — Ctrl+C to stop.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped.")
