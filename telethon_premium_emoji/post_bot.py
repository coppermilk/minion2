"""Telethon BOT: a post with inline premium emoji and a bottom button.

Reproduces the channel-post look: premium custom emoji inline in the text
(colored bullets) plus one full-width inline button (the "plate") at the
bottom, like a VIDEO call-to-action.

Only a BOT can attach that bottom button -- inline keyboards are bot-only --
so this logs in with a bot token, not a user session. Sending premium emoji
(in the text or as the button icon) needs the bot's OWNER to have Telegram
Premium. The web-page preview is turned off so no link card covers the post.

Env: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_BOT_TOKEN, TARGET_CHAT_ID,
and optional BUTTON_TEXT / BUTTON_URL (set BUTTON_TEXT in .env for a non-ASCII
label like a Russian caption -- the source itself stays ASCII).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from premium_emoji import build_premium_message
from telethon import TelegramClient
from telethon.tl.types import KeyboardButtonRow
from telethon.tl.types import KeyboardButtonUrl
from telethon.tl.types import ReplyInlineMarkup

try:
    from telethon.tl.types import KeyboardButtonStyle

    _HAVE_STYLE = True
except ImportError:  # Telethon predates the Bot API 9.4 button layer
    _HAVE_STYLE = False

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(name)s: %(message)s',
)
log = logging.getLogger('post-bot')

DEFAULT_TARGET_CHAT_ID = -1002431466060

# A premium custom-emoji document id. Override it per run with EMOJI_ID in
# .env, so you can try ids from dump_emoji_ids.py without editing code. It is
# reused for every premium emoji in the post and the button icon.
DEFAULT_EMOJI_ID = 5330248916224983855

# The plain glyph prefixed to the button label when the icon cannot render.
BUTTON_FALLBACK = '\U0001f3ac'  # clapper board


def _emoji_id() -> int:
    """The premium emoji id to use (EMOJI_ID in .env overrides the default)."""
    return int(os.environ.get('EMOJI_ID', DEFAULT_EMOJI_ID))


def _post_markup(emoji_id: int) -> str:
    """The post body; a red-circle fallback shows if premium does not render."""
    tag = f'<tg-emoji emoji-id="{emoji_id}">\U0001f534</tg-emoji>'
    return (
        f'{tag} Premium emoji in the post (proof-of-work).\n\n'
        f'{tag} First bullet with a premium icon\n'
        f'{tag} Second bullet with a premium icon'
    )


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


def _button(emoji_id: int) -> KeyboardButtonUrl:
    """The single bottom button, with a premium emoji icon when supported."""
    text = os.environ.get('BUTTON_TEXT', 'VIDEO')
    url = os.environ.get('BUTTON_URL', 'https://www.youtube.com/')
    if _HAVE_STYLE:
        style = KeyboardButtonStyle(icon=emoji_id)
        return KeyboardButtonUrl(text=text, url=url, style=style)
    return KeyboardButtonUrl(text=f'{BUTTON_FALLBACK} {text}', url=url)


def _markup(emoji_id: int) -> ReplyInlineMarkup:
    """A one-button inline keyboard, so the button spans the full width."""
    return ReplyInlineMarkup(
        rows=[KeyboardButtonRow(buttons=[_button(emoji_id)])]
    )


async def send_post(client: TelegramClient, chat_id: int) -> None:
    """Post the premium-emoji text with the bottom plate; no link preview."""
    emoji_id = _emoji_id()
    message = build_premium_message(_post_markup(emoji_id))
    await client.send_message(
        chat_id,
        message.text,
        formatting_entities=message.entities,
        buttons=_markup(emoji_id),
        link_preview=False,
    )
    log.info('Posted to %s (premium emoji id %s).', chat_id, emoji_id)


async def main() -> None:
    """Log in as a bot, post the premium-emoji message, then disconnect."""
    _load_dotenv(Path(__file__).with_name('.env'))

    api_id = os.environ.get('TELEGRAM_API_ID')
    api_hash = os.environ.get('TELEGRAM_API_HASH')
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not api_id or not api_hash or not token:
        raise SystemExit(
            'Set TELEGRAM_API_ID, TELEGRAM_API_HASH and TELEGRAM_BOT_TOKEN '
            '(the bot token from @BotFather).'
        )
    chat_id = int(os.environ.get('TARGET_CHAT_ID', DEFAULT_TARGET_CHAT_ID))

    client = TelegramClient('post_bot', int(api_id), api_hash)
    await client.start(bot_token=token)

    me = await client.get_me()
    if not me.bot:
        raise SystemExit('This token is not a bot -- the button is bot-only.')
    if not _HAVE_STYLE:
        log.warning(
            'This Telethon has no KeyboardButtonStyle (Bot API 9.4); the '
            'button icon falls back to a plain glyph in its label.'
        )
    log.info('Logged in as bot @%s.', me.username or me.id)

    await send_post(client, chat_id)
    await client.disconnect()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info('Stopped.')
