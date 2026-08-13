# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Log in once and create the donate userbot's session FILE, then exit.

Run this ONCE interactively (it asks for the phone, the login code Telegram
sends, and the 2FA password if the account has one). It writes the Telethon
session file -- full account access -- and prints where it wrote it:

    python -m minions.donate.login

Use the SEPARATE account this shout-out bot runs on (not the aggregator's): a
Telethon session is one account, so this login creates its own. By default the
file is ``telethon.session`` next to this package; set ``DONATE_SESSION_FILE``
to write it elsewhere (e.g. ``/data/bots/donate/session`` for the container's
persistent mount -- ``.session`` is appended for you). After it exists,
``python -m minions.donate.main`` logs in silently on every start.

The 2FA password only authorises this login; it is NOT stored in the session
file. Treat the ``.session`` file like a password -- it is full account access.
It is git-ignored; don't commit or share it, and revoke it from Telegram ->
Settings -> Devices if it leaks.
"""

from __future__ import annotations

import os

from telethon import TelegramClient

from minions.donate.main import _resolve_session_path
from minions.donate.main import load_env


def main() -> None:
    """Log in once interactively and write the session file."""
    load_env()

    api_id = os.environ.get('DONATE_API_ID')
    api_hash = os.environ.get('DONATE_API_HASH')
    if not api_id or not api_hash:
        msg = (
            'Set DONATE_API_ID and DONATE_API_HASH first '
            '(in .env or the environment).'
        )
        raise SystemExit(msg)

    session_path = _resolve_session_path()
    session_path.parent.mkdir(parents=True, exist_ok=True)

    # DONATE_PASSWORD supplies the 2FA password non-interactively; unset,
    # Telethon prompts for it (getpass) only if the account has 2FA enabled.
    start_kwargs: dict[str, object] = {}
    password = os.environ.get('DONATE_PASSWORD')
    if password:
        start_kwargs['password'] = password

    # start() runs the login and writes the .session file on disk.
    with TelegramClient(str(session_path), int(api_id), api_hash) as client:
        client.start(**start_kwargs)
        me = client.get_me()

    print()
    print(f'Logged in as @{me.username or "-"} (id={me.id}).')
    print('=' * 70)
    print(f'Session file written: {session_path}.session')
    print('Keep it secret -- it is full account access (git-ignored).')
    print('=' * 70)
    print(
        'Copy this file to where the donate bot runs (or set '
        'DONATE_SESSION_FILE), then `python -m minions.donate.main` '
        'logs in silently.'
    )


if __name__ == '__main__':
    main()
