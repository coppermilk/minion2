# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""donate userbot: render a donation shout-out on /donate, on its OWN account.

A Telethon USER account (only a user account may send premium emoji), separate
from the aggregator: one Telethon session is one account, so a second account
needs a second app. This one does a single thing -- listen for
``/donate <name> <amount> <message>`` and render a formatted shout-out: a
leading premium emoji, the donor and the amount, their quoted message, a
reaction line, and a footer of links.

Every text and the premium emoji live in ``donate_constants.json`` (UTF-8), so
this source stays pure ASCII (BLUEPRINT 4). The donor name and message ride as
plain text with explicit MTProto entities (no HTML parse mode), so there is
nothing to escape or inject.

Env (all DONATE_-prefixed, so they never collide with the aggregator's):
    DONATE_API_ID, DONATE_API_HASH  -- the account's API credentials
    DONATE_PASSWORD                 -- optional 2FA/cloud password
    DONATE_SESSION_FILE             -- session-file path override
    DONATE_CHAT_ID                  -- where to render the shout-out; unset, it
                                       renders in the chat the command arrived
The session defaults to <DRIVE>/bots/donate/telethon (DRIVE is the library root
-- your Google Drive on Windows, /data in the NAS container), else next to this
package. Run ``python -m minions.donate.login`` once to create the session,
then ``python -m minions.donate.main``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from telethon import TelegramClient
from telethon import events

from minions.aggregator.premium_emoji import RichText

if TYPE_CHECKING:
    from minions.aggregator.premium_emoji import PremiumMessage

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(name)s: %(message)s',
)
log = logging.getLogger('donate')

BOT = 'donate'
COMMAND = '/donate'
CONSTANTS_FILE = 'donate_constants.json'
# Last-resort file-session base path: 'telethon.session' next to this package
# (git-ignored, like the aggregator's, so a checkout re-sync keeps it).
DEFAULT_SESSION_PATH = Path(__file__).with_name('telethon')
# The project keeps ONE .env at the repo root; this package is minions/donate/,
# so parents[2] is that root. In Docker the vars are already in os.environ.
PROJECT_ENV = Path(__file__).resolve().parents[2] / '.env'


@dataclass(frozen=True)
class Consts:
    """Editable texts and the leading premium emoji, loaded from JSON."""

    emoji: object
    header: str
    currency: str
    quote: str
    reactions: list[str]
    separator: str
    arrow: str
    link_text: str
    links: list[dict[str, str]]
    usage: str


@dataclass(frozen=True)
class Donation:
    """One /donate request parsed from the command text."""

    name: str
    amount: str
    message: str


def _read_json(path: Path) -> dict[str, object]:
    """Parse the constants JSON; on a bad/missing file, log and use defaults.

    A typo in donate_constants.json must not take the bot down: log a clear
    one-line error naming the file, then fall back to built-in defaults so the
    bot still starts (shout-outs are bland until it is fixed).
    """
    # A config typo wants a line, not a trace: log at error level without the
    # stack, AFTER the except (so no exception context attaches). Both bad
    # cases -- parse failure and not-an-object -- share one error path.
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        reason = f'{path.name} is invalid ({exc})'
    else:
        if isinstance(data, dict):
            return data
        reason = f'{path.name} must be a JSON object'
    log.error('%s; using defaults -- fix it and restart.', reason)
    return {}


def _load_constants(path: Path) -> Consts:
    """Load the shout-out constants from JSON, ignoring unknown keys."""
    data = _read_json(path)
    reactions = [str(r) for r in (data.get('donate_reactions') or [''])]
    return Consts(
        emoji=data.get('donate_emoji') or '',
        header=str(data.get('donate_header') or '{name}: {amount}'),
        currency=str(data.get('donate_currency', '')),
        quote=str(data.get('donate_quote') or '{message}'),
        reactions=reactions or [''],
        separator=str(data.get('donate_separator', '')),
        arrow=str(data.get('donate_arrow') or ' -> '),
        link_text=str(data.get('donate_link_text') or 'link'),
        links=[dict(row) for row in (data.get('donate_links') or [])],
        usage=str(
            data.get('donate_usage')
            or 'Usage: /donate <name> <amount> <message>'
        ),
    )


_DONATE_FIELDS = 4
"""/donate splits into cmd, name, amount, message."""


def _parse(text: str) -> Donation | None:
    """Split '/donate <name> <amount> <message>' into its three parts.

    The command word, name and amount are single tokens; everything after is
    the (possibly multi-word) message. Returns None if any part is missing.
    """
    parts = text.split(maxsplit=3)
    if len(parts) < _DONATE_FIELDS:
        return None
    return Donation(name=parts[1], amount=parts[2], message=parts[3])


def _is_command(low: str) -> bool:
    """Whether the lowercased text is /donate (bare or with args)."""
    return low == COMMAND or low.startswith(COMMAND + ' ')


def _amount(amount: str, currency: str) -> str:
    """Return the amount with the currency symbol (no double)."""
    if currency and not amount.endswith(currency):
        return amount + currency
    return amount


def _compose(donation: Donation, consts: Consts) -> PremiumMessage:
    """Build the shout-out: header, quoted message, reaction, and link footer.

    The leading premium emoji and every text come from the constants JSON, so
    the look is edited there without touching code.
    """
    amount = _amount(donation.amount, consts.currency)
    header = consts.header.format(name=donation.name, amount=amount)
    quote = consts.quote.format(message=donation.message)
    reaction = random.choice(consts.reactions).format(  # noqa: S311
        name=donation.name
    )
    rich = RichText()
    rich.emoji(consts.emoji).text(' ')
    rich.text(header).text('\n\n')
    rich.text(quote).text('\n\n')
    rich.text(reaction).text('\n\n\n')
    rich.text(consts.separator).text('\n')
    for row in consts.links:
        rich.text(str(row.get('label', '')) + consts.arrow)
        rich.link(consts.link_text, str(row.get('url', '')))
        rich.text('\n')
    return rich.build()


class DonateBot:
    """Listen for /donate and render the shout-out to the configured chat."""

    def __init__(self, client: TelegramClient, chat: int | None) -> None:
        """Keep the client and target chat; load the texts."""
        self.client = client
        self.chat = chat  # fixed target, or None to reply where invoked
        self.consts = _load_constants(Path(__file__).with_name(CONSTANTS_FILE))

    async def handle(self, event: events.NewMessage.Event) -> None:
        """Render a shout-out for a /donate message; ignore everything else."""
        text = (event.raw_text or '').strip()
        if not _is_command(text.lower()):
            return
        target = self.chat if self.chat is not None else event.chat_id
        donation = _parse(text)
        if donation is None:
            await self.client.send_message(target, self.consts.usage)
            log.info('donate: malformed command, sent usage')
            return
        message = _compose(donation, self.consts)
        await self.client.send_message(
            target,
            message.text,
            formatting_entities=message.entities,
            link_preview=False,
        )
        log.info('donate: rendered shout-out for %r', donation.name)


def _load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE lines from a .env file (environment wins)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        os.environ.setdefault(key.strip(), value.strip().strip('\'"'))


def load_env() -> None:
    """Load the project's root .env so a bare run finds the credentials."""
    _load_dotenv(PROJECT_ENV)


def _drive_dir() -> Path | None:
    """Return the donate data dir <DRIVE>/bots/donate, or None."""
    drive = os.environ.get('DRIVE')
    if not drive:
        return None
    return Path(drive).expanduser() / 'bots' / BOT


def _resolve_session_path() -> Path:
    """Return the session base path (override, <DRIVE>, or package)."""
    override = os.environ.get('DONATE_SESSION_FILE')
    if override:
        path = Path(override).expanduser()
        return path.with_suffix('') if path.suffix == '.session' else path
    drive = _drive_dir()
    return drive / 'telethon' if drive is not None else DEFAULT_SESSION_PATH


def _chat() -> int | None:
    """Return the fixed target from DONATE_CHAT_ID, or None for in-place."""
    raw = os.environ.get('DONATE_CHAT_ID')
    return int(raw) if raw else None


async def main() -> None:
    """Listen for /donate and render the shout-out (idle until it arrives)."""
    load_env()

    api_id = os.environ.get('DONATE_API_ID')
    api_hash = os.environ.get('DONATE_API_HASH')
    if not api_id or not api_hash:
        msg = 'Set DONATE_API_ID and DONATE_API_HASH.'
        raise SystemExit(msg)

    session_path = _resolve_session_path()
    session_path.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(session_path), int(api_id), api_hash)
    bot = DonateBot(client, _chat())
    client.add_event_handler(bot.handle, events.NewMessage())

    # DONATE_PASSWORD supplies the 2FA/cloud password non-interactively;
    # unset, Telethon prompts for it (getpass) only if the account has 2FA.
    start_kwargs: dict[str, object] = {}
    password = os.environ.get('DONATE_PASSWORD')
    if password:
        start_kwargs['password'] = password
    log.info('Session store: %s.session', session_path)
    await client.start(**start_kwargs)
    log.info(
        'donate userbot listening; rendering to %s',
        bot.chat if bot.chat is not None else 'the command chat',
    )
    await client.run_until_disconnected()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info('Stopped.')
