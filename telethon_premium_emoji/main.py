"""Telethon user-account app that posts a premium-emoji proof-of-work on start.

This is a *userbot* -- it logs in as a real Telegram account over MTProto, not
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

from premium_emoji import Social
from premium_emoji import build_premium_message
from premium_emoji import build_social_bar
from telethon import TelegramClient
from telethon.sessions import StringSession

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(name)s: %(message)s',
)
log = logging.getLogger('premium-emoji')

# Chat the proof-of-work goes to (the -100... supergroup id from the task).
DEFAULT_TARGET_CHAT_ID = -1002431466060

# Proof-of-work payload. The example premium emoji from the task, plus a line
# of context so it reads as a deliberate startup ping rather than noise.
PROOF_OF_WORK_MARKUP = (
    '<tg-emoji emoji-id="5334681713316479679">\U0001f4f1</tg-emoji> '
    'Telethon userbot online -- premium emoji proof-of-work.'
)

# Colored "buttons": a row of premium platform logos, each linked to its
# platform. The COLOR is the premium emoji itself (black TikTok, red
# YouTube, pink Instagram, red Pinterest). A user account cannot send real
# inline buttons and buttons never render premium emoji, so this row of
# linked premium emoji is the userbot equivalent.
#
# emoji_id is the premium custom-emoji document id. For now every entry
# reuses the one id we have (Instagram's), so the whole bar renders as a
# premium emoji already -- swap TikTok/YouTube/Pinterest for their own ids
# when you have them to get the exact colored logos. Point the urls at your
# own profiles.
INSTAGRAM_EMOJI_ID = 5319160079465857105
SOCIAL_BAR = (
    Social(
        'TikTok', INSTAGRAM_EMOJI_ID, '\U0001f3b5', 'https://www.tiktok.com/'
    ),
    Social(
        'YouTube', INSTAGRAM_EMOJI_ID, '\U000025b6', 'https://www.youtube.com/'
    ),
    Social(
        'Instagram',
        INSTAGRAM_EMOJI_ID,
        '\U0001f4f7',
        'https://www.instagram.com/',
    ),
    Social(
        'Pinterest',
        INSTAGRAM_EMOJI_ID,
        '\U0001f4cc',
        'https://www.pinterest.com/',
    ),
)


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so a local file can supply credentials.

    Kept dependency-free on purpose; real values still come from the process
    environment when both are present (the environment wins).
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        os.environ.setdefault(key.strip(), value.strip().strip('\'"'))


# Default file-session location: anchored next to this script (not the
# current working directory) so the same file is reused no matter where
# you launch from. Telethon appends ".session", so the file on disk is
# "telethon_premium_emoji.session". Override with TELEGRAM_SESSION_FILE
# to point at a path that survives shutdowns (e.g. a mounted volume).
DEFAULT_SESSION_PATH = Path(__file__).with_name('telethon_premium_emoji')


def _resolve_session_path() -> Path:
    """The file-session base path, from TELEGRAM_SESSION_FILE or the default.

    A trailing ".session" is stripped so the value works whether you point at
    the file itself or its base name.
    """
    override = os.environ.get('TELEGRAM_SESSION_FILE')
    if not override:
        return DEFAULT_SESSION_PATH
    path = Path(override).expanduser()
    if path.suffix == '.session':
        path = path.with_suffix('')
    return path


def _build_client() -> tuple[TelegramClient, str]:
    """Build the client; also return a human description of the session."""
    api_id = os.environ.get('TELEGRAM_API_ID')
    api_hash = os.environ.get('TELEGRAM_API_HASH')
    if not api_id or not api_hash:
        raise SystemExit(
            'Set TELEGRAM_API_ID and TELEGRAM_API_HASH '
            '(get them from https://my.telegram.org).'
        )

    # StringSession stays supported as an opt-in, but the default (and what we
    # recommend here) is a plain session FILE that persists on disk across
    # restarts -- log in once, then every later start is silent.
    session_str = os.environ.get('TELEGRAM_SESSION')
    if session_str:
        session: StringSession | str = StringSession(session_str)
        where = 'in-memory StringSession (from TELEGRAM_SESSION)'
    else:
        session_path = _resolve_session_path()
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session = str(session_path)
        where = f'{session_path}.session'
    return TelegramClient(session, int(api_id), api_hash), where


async def send_proof_of_work(client: TelegramClient, chat_id: int) -> None:
    """Send the premium-emoji proof-of-work message to ``chat_id``."""
    message = build_premium_message(PROOF_OF_WORK_MARKUP)
    await client.send_message(
        chat_id, message.text, formatting_entities=message.entities
    )
    log.info(
        'Sent proof-of-work to %s with %d premium emoji entit%s.',
        chat_id,
        len(message.entities),
        'y' if len(message.entities) == 1 else 'ies',
    )


async def send_social_bar(client: TelegramClient, chat_id: int) -> None:
    """Send the colored premium-emoji social bar to ``chat_id``."""
    message = build_social_bar(SOCIAL_BAR)
    await client.send_message(
        chat_id, message.text, formatting_entities=message.entities
    )
    log.info(
        'Sent social bar to %s with %d buttons.', chat_id, len(SOCIAL_BAR)
    )


async def main() -> None:
    """Log in, verify it is a user account, send the proof-of-work."""
    _load_dotenv(Path(__file__).with_name('.env'))
    chat_id = int(os.environ.get('TARGET_CHAT_ID', DEFAULT_TARGET_CHAT_ID))

    client, session_where = _build_client()
    log.info('Session store: %s', session_where)

    # First run performs the login: phone number, the code Telegram sends,
    # and -- if the account has 2FA (a cloud password) -- that password too.
    # Telethon uses the password ONLY to authenticate; it is never written
    # to the session. After a successful login the auth key is saved and
    # later runs are silent.
    #
    # TELEGRAM_PASSWORD supplies the 2FA password non-interactively (e.g. a
    # server/cron run). If unset, Telethon prompts for it securely (getpass,
    # no echo) only when the account actually has 2FA enabled.
    start_kwargs: dict[str, object] = {}
    password = os.environ.get('TELEGRAM_PASSWORD')
    if password:
        start_kwargs['password'] = password
    await client.start(**start_kwargs)

    me = await client.get_me()
    if me.bot:
        raise SystemExit(
            'Logged in as a BOT account -- premium emoji need a user account. '
            'Use a real user session, not a bot token.'
        )
    if not getattr(me, 'premium', False):
        log.warning(
            'Account @%s is not Telegram Premium; the custom emoji may render '
            'as its fallback glyph for you but will still send.',
            me.username or me.id,
        )

    log.info('Logged in as @%s (id=%s).', me.username or '-', me.id)
    await send_proof_of_work(client, chat_id)
    await send_social_bar(client, chat_id)

    log.info('Proof-of-work done. Staying connected -- Ctrl+C to stop.')
    await client.run_until_disconnected()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info('Stopped.')
