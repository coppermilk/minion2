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
from pathlib import Path

from telethon import TelegramClient
from telethon import events
from telethon.tl.types import MessageEntityCustomEmoji

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(name)s: %(message)s',
)
log = logging.getLogger('dump-emoji-ids')


def _load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE lines from a local .env (environment wins)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        os.environ.setdefault(key.strip(), value.strip().strip('\'"'))


def _glyph(text: str, offset: int, length: int) -> str:
    """The fallback glyph a custom-emoji entity covers (UTF-16 units)."""
    raw = text.encode('utf-16-le')
    return raw[offset * 2 : (offset + length) * 2].decode('utf-16-le')


async def _report(event: events.NewMessage.Event) -> None:
    """Log the document_id of each custom emoji in the message."""
    found = False
    for entity in event.message.entities or []:
        if isinstance(entity, MessageEntityCustomEmoji):
            found = True
            glyph = _glyph(event.message.message, entity.offset, entity.length)
            log.info(
                'premium emoji: emoji-id="%s" (fallback glyph %r)',
                entity.document_id,
                glyph,
            )
    if not found:
        log.info('No premium emoji in that message.')


async def main() -> None:
    """Listen for your messages and print each premium emoji's id."""
    _load_dotenv(Path(__file__).with_name('.env'))

    api_id = os.environ.get('TELEGRAM_API_ID')
    api_hash = os.environ.get('TELEGRAM_API_HASH')
    if not api_id or not api_hash:
        raise SystemExit('Set TELEGRAM_API_ID and TELEGRAM_API_HASH.')

    client = TelegramClient('telethon_premium_emoji', int(api_id), api_hash)
    client.add_event_handler(_report, events.NewMessage(outgoing=True))

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
