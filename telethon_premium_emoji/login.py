"""One-time login → prints a StringSession you paste into .env, then never
log in again.

Run this ONCE interactively (it asks for phone, the login code, and your 2FA
password if you have one). It prints a long `TELEGRAM_SESSION=…` line. Put that
line in your .env and from then on `main.py` logs in silently on every start —
survives reboots, machine shutdowns, moving to another host — because the auth
key now lives in that string, not on the machine's disk.

    python login.py

The password is used only to authorise this login; it is NOT stored in the
session string. Treat the printed string like a password — it is full access
to the account. Don't commit it or share it.
"""

from __future__ import annotations

import os
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main() -> None:
    _load_dotenv(Path(__file__).with_name(".env"))

    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit(
            "Set TELEGRAM_API_ID and TELEGRAM_API_HASH first "
            "(in .env or the environment)."
        )

    # StringSession() starts empty; after start() it holds the new auth key.
    with TelegramClient(StringSession(), int(api_id), api_hash) as client:
        me = client.get_me()
        session_string = client.session.save()

    print()
    print("Logged in as @%s (id=%s)." % (me.username or "-", me.id))
    print("=" * 70)
    print("Add this line to your .env (keep it secret — it is account access):")
    print()
    print("TELEGRAM_SESSION=%s" % session_string)
    print("=" * 70)
    print("After that, `python main.py` logs in silently on every start.")


if __name__ == "__main__":
    main()
