# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Print the document_id of every premium (custom) emoji you send.

Run this with your Telegram USER account (the Premium one). Then send or
forward any message containing premium emoji to your own Saved Messages (or
type it in any chat this account sees). For each custom emoji the script
prints its document_id -- the exact value to paste as an emoji-id. This ends
the guessing: an id printed here is a real, valid, renderable custom-emoji id.

Reuses the same file session as main.py, so log in once.

Env: TELEGRAM_API_ID, TELEGRAM_API_HASH.
"""

from __future__ import annotations

import asyncio
import logging
import os

from minion_core.adapters import userchat
from minion_core.richtext import EMOJI
from minions.userbot.core.config import load_env
from minions.userbot.core.config import resolve_session_path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(name)s: %(message)s',
)
log = logging.getLogger('dump-emoji-ids')


async def _report(msg: userchat.Msg) -> None:
    """Log the emoji id of each custom emoji in the message."""
    found = False
    for span in msg.spans:
        if span.kind == EMOJI:
            found = True
            log.info(
                'premium emoji: emoji-id="%s" (fallback glyph %r)',
                span.ref,
                msg.text[span.at : span.at + span.length],
            )
    if not found:
        log.info('No premium emoji in that message.')


async def main() -> None:
    """Listen for your messages and print each premium emoji's id."""
    load_env()

    api_id = os.environ.get('TELEGRAM_API_ID')
    api_hash = os.environ.get('TELEGRAM_API_HASH')
    if not api_id or not api_hash:
        msg = 'Set TELEGRAM_API_ID and TELEGRAM_API_HASH.'
        raise SystemExit(msg)

    session_path = resolve_session_path()
    session_path.parent.mkdir(parents=True, exist_ok=True)
    # Through connect(), not a bare TelegramClient: this opens the SAME
    # session file the bot uses, and opening it without the WAL journal is
    # how that file gets corrupted.
    client = userchat.connect(
        userchat.Login(session_path, int(api_id), api_hash)
    )
    userchat.Account(client, userchat.paces({})).on_message(_report)

    await client.start()
    log.info(
        'Listening. Send a message with premium emoji to your Saved Messages '
        '-- Ctrl+C to stop.'
    )
    await client.run_until_disconnected()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info('Stopped.')
