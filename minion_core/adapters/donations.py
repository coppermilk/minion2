"""Donation feeds: platform-agnostic donor alerts (requests).

One value type (``Donation``) and one ``Feed`` protocol; each platform
is one adapter behind it. Streamlabs is the first feed; Revolut and any
other platform slot in as a new ``Feed`` class plus one line in
``feed_for`` -- the belt, the source and the Russian render never
change. Sole Streamlabs importer of ``requests`` (REQ-ARC-002); the
lazy import keeps the module hermetic for the offline suite.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Protocol

from minion_core.adapters.tg import OffsetStore
from minion_core.adapters.tg import TgApi
from minion_core.kernel import Source

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Mapping
    from pathlib import Path

    from minion_core.kernel import Emit

_LOG = logging.getLogger('donations')

API_TIMEOUT_SEC = 90
"""Wall-time bound on every HTTP call (bounded, BLUEPRINT 10)."""

STREAMLABS_LIMIT = 100
"""How many recent donations one Streamlabs page returns."""

DEFAULT_POLL_SEC = 10.0
"""Gap between polls; a paid external API is not hammered."""

STREAMLABS = 'streamlabs'
"""The default platform name (the agnostic default)."""

REVOLUT = 'revolut'
"""The second wired platform: Revolut Business incoming transactions."""

REVOLUT_LIMIT = 100
"""How many recent transactions one Revolut page returns."""


@dataclass(frozen=True)
class Donation:
    """One donor alert, transport-neutral: who, how much, the question.

    ``ident`` is the feed's monotonic high-water key (Streamlabs'
    donation id); ``amount`` is already formatted with its currency.
    """

    ident: int
    platform: str
    name: str
    amount: str
    currency: str
    message: str


class Feed(Protocol):
    """A donation platform: newer alerts since a cursor."""

    @property
    def name(self) -> str:
        """The platform label shown in each alert."""

    @property
    def live(self) -> bool:
        """Whether the feed is configured enough to poll."""

    def fetch_after(self, cursor: int, /) -> list[Donation]:
        """Alerts whose id is past ``cursor`` (oldest first)."""


def _int(value: object) -> int | None:
    """Coerce an id that may arrive as a JSON string or a number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _text(value: object) -> str:
    """A trimmed string field, or '' when absent or blank."""
    return value.strip() if isinstance(value, str) else ''


def _money(value: object) -> str:
    """The donated amount as a bare number string (may be numeric)."""
    if isinstance(value, bool):
        return ''
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int | float):
        return str(value)
    return ''


def _one(row: Mapping[str, object], platform: str) -> Donation | None:
    """One donation row, or None when its id is missing or bad.

    Amount and currency are kept apart so the render picks the symbol
    (RUB -> the ruble sign, USD -> a dollar sign, ...) from its own
    table; the adapter stays currency-blind.
    """
    ident = _int(row.get('donation_id'))
    if ident is None:
        return None
    return Donation(
        ident=ident,
        platform=platform,
        name=_text(row.get('name')),
        amount=_money(row.get('amount')),
        currency=_text(row.get('currency')),
        message=_text(row.get('message')),
    )


def _parse(payload: object, cursor: int, platform: str) -> list[Donation]:
    """Read the untrusted donations payload into alerts (BLUEPRINT 4).

    Every field is validated explicitly: a malformed row is dropped,
    never a crash, and only ids strictly past ``cursor`` survive so an
    alert is posted exactly once.
    """
    data = payload.get('data') if isinstance(payload, dict) else None
    rows = data if isinstance(data, list) else []
    parsed = (_one(r, platform) for r in rows if isinstance(r, dict))
    fresh = [d for d in parsed if d is not None and d.ident > cursor]
    return sorted(fresh, key=lambda d: d.ident)


@dataclass(frozen=True)
class StreamlabsFeed:
    """Streamlabs donations, newest since a donation-id cursor."""

    token: str
    base: str = 'https://streamlabs.com'

    @property
    def name(self) -> str:
        """The platform label shown in each alert."""
        return 'Streamlabs'

    @property
    def live(self) -> bool:
        """Whether an API access token is configured."""
        return bool(self.token)

    def fetch_after(self, cursor: int, /) -> list[Donation]:
        """Donations with an id past ``cursor`` (oldest first)."""
        import requests

        params: dict[str, str] = {
            'access_token': self.token,
            'limit': str(STREAMLABS_LIMIT),
        }
        if cursor:
            params['after'] = str(cursor)
        resp = requests.get(
            f'{self.base}/api/v1.0/donations',
            params=params,
            timeout=API_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        return _parse(resp.json(), cursor, self.name)


def _epoch_ms(value: object) -> int | None:
    """An ISO-8601 timestamp as a millisecond cursor, or None.

    Revolut transactions carry no integer id, so the completion time --
    to the millisecond -- is the monotonic high-water key the belt uses.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return int(moment.timestamp() * 1000)


def _num(value: object) -> float:
    """A numeric amount, or 0.0 when absent or non-numeric."""
    if isinstance(value, bool):
        return 0.0
    return float(value) if isinstance(value, int | float) else 0.0


def _money_str(amount: float) -> str:
    """A tidy amount string: no trailing ``.0`` on whole numbers."""
    return str(int(amount)) if amount == int(amount) else str(amount)


def _incoming_leg(legs: object) -> dict[str, object] | None:
    """The first credit leg (money in), or None -- an outgoing/zero tx."""
    rows = legs if isinstance(legs, list) else []
    for leg in rows:
        if isinstance(leg, dict) and _num(leg.get('amount')) > 0:
            return leg
    return None


def _counterparty(leg: dict[str, object]) -> str:
    """The donor's name from a transaction leg, or ''."""
    party = leg.get('counterparty')
    name = party.get('name') if isinstance(party, dict) else None
    return name.strip() if isinstance(name, str) else ''


def _revolut_one(tx: dict[str, object], platform: str) -> Donation | None:
    """One completed incoming transaction as a Donation, or None."""
    if tx.get('state') != 'completed':
        return None
    ident = _epoch_ms(tx.get('completed_at') or tx.get('created_at'))
    leg = _incoming_leg(tx.get('legs'))
    if ident is None or leg is None:
        return None
    return Donation(
        ident=ident,
        platform=platform,
        name=_counterparty(leg),
        amount=_money_str(_num(leg.get('amount'))),
        currency=_text(leg.get('currency')),
        message=_text(tx.get('reference')) or _text(leg.get('description')),
    )


def _parse_revolut(
    payload: object, cursor: int, platform: str
) -> list[Donation]:
    """Read the untrusted transactions payload into alerts (BLUEPRINT 4).

    Only completed, incoming transactions past ``cursor`` survive, so a
    top-up or a refund is never mistaken for a donation and each is
    posted exactly once.
    """
    rows = payload if isinstance(payload, list) else []
    parsed = (_revolut_one(r, platform) for r in rows if isinstance(r, dict))
    fresh = [d for d in parsed if d is not None and d.ident > cursor]
    return sorted(fresh, key=lambda d: d.ident)


@dataclass(frozen=True)
class RevolutFeed:
    """Revolut Business incoming transactions, newest since a cursor.

    The token is a Revolut Business API access token (Bearer). Those
    tokens are short-lived, so a long-running deployment refreshes it out
    of band; the OAuth refresh flow is deliberately out of scope here.
    """

    token: str
    base: str = 'https://b2b.revolut.com'

    @property
    def name(self) -> str:
        """The platform label shown in each alert."""
        return 'Revolut'

    @property
    def live(self) -> bool:
        """Whether an API access token is configured."""
        return bool(self.token)

    def fetch_after(self, cursor: int, /) -> list[Donation]:
        """Incoming transactions past ``cursor`` (oldest first)."""
        import requests

        resp = requests.get(
            f'{self.base}/api/1.0/transactions',
            headers={'Authorization': f'Bearer {self.token}'},
            params={'count': str(REVOLUT_LIMIT)},
            timeout=API_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        return _parse_revolut(resp.json(), cursor, self.name)


@dataclass(frozen=True)
class DeadFeed:
    """A feed that never yields; the clean-degradation default.

    An unknown or unset platform becomes one of these, so the bot idles
    instead of crashing (REQ-DEG-001).
    """

    platform: str

    @property
    def name(self) -> str:
        """The requested platform label."""
        return self.platform

    @property
    def live(self) -> bool:
        """Never live: the belt idles."""
        return False

    def fetch_after(self, _cursor: int, /) -> list[Donation]:
        """No alerts, ever."""
        return []


def feed_for(platform: str, env: Mapping[str, str]) -> Feed:
    """Build the feed for a platform name -- the agnostic registry.

    Add a platform here and nowhere else: one ``Feed`` class plus one
    branch. An unknown or unset platform yields an inert feed.
    """
    if platform == STREAMLABS:
        return StreamlabsFeed(env.get('STREAMLABS_TOKEN', ''))
    if platform == REVOLUT:
        return RevolutFeed(env.get('REVOLUT_TOKEN', ''))
    return DeadFeed(platform)


def feeds_for(platforms: str, env: Mapping[str, str]) -> list[Feed]:
    """Every feed named in a comma-separated platform list.

    ``streamlabs,revolut`` runs both at once, each with its own cursor;
    order and whitespace do not matter. An empty list yields no feeds
    (the bot idles).
    """
    names = [part.strip() for part in platforms.split(',') if part.strip()]
    return [feed_for(name, env) for name in names]


class Sender(Protocol):
    """A one-way text sink toward a chat; live when configured."""

    @property
    def live(self) -> bool:
        """Whether the transport can actually post."""

    def send(self, chat: str, text: str) -> None:
        """Post ``text`` to ``chat``."""


@dataclass(frozen=True)
class TgSender:
    """Post text to a Telegram chat via the Bot API.

    ``parse_mode`` (e.g. ``HTML``) turns on rich formatting -- italics,
    links, a monospace block for the ASCII art. ``preview`` off keeps the
    tip link from expanding into a card below every alert.
    """

    api: TgApi
    parse_mode: str = ''
    preview: bool = True

    @property
    def live(self) -> bool:
        """Whether the bot token is set."""
        return self.api.live

    def send(self, chat: str, text: str) -> None:
        """Send one message; a missing token or chat is a no-op."""
        if not self.api.live or not chat:
            return
        params: dict[str, object] = {'chat_id': chat, 'text': text}
        if self.parse_mode:
            params['parse_mode'] = self.parse_mode
        if not self.preview:
            params['disable_web_page_preview'] = True
        self.api.call('sendMessage', params)


@dataclass(frozen=True)
class AlertSpec:
    """Where alerts post, how often, and how each is rendered.

    ``state`` is the directory the per-feed high-water files live in, so
    Streamlabs and Revolut keep independent cursors (their id scales --
    a small donation id vs a millisecond timestamp -- must never share).
    """

    chat: str
    state: Path
    render: Callable[[Donation], str]
    poll_sec: float = DEFAULT_POLL_SEC


class DonationAlerts(Source):
    """Poll one or more donation feeds; post each new alert to a chat.

    Platform-agnostic: any ``Feed`` drives it, and several run at once
    (Streamlabs and Revolut together). Each feed keeps its own high-water
    mark, persisted per post (STATE, REQ-DATA-003), so a restart never
    replays an alert and one platform's cursor never shadows another's.
    Unconfigured, it ends at once and degrades to a clean no-op
    (REQ-DEG-001).
    """

    def __init__(
        self, feeds: list[Feed], sender: Sender, spec: AlertSpec
    ) -> None:
        super().__init__()
        self._feeds = feeds
        self._sender = sender
        self._spec = spec

    def drain_once(self) -> None:
        """One poll of every live feed, each on its own cursor."""
        for feed in self._feeds:
            if feed.live:
                self._drain_feed(feed)

    def _drain_feed(self, feed: Feed) -> None:
        """Post every alert a feed has past its persisted high-water."""
        offsets = OffsetStore(self._offset(feed.name))
        cursor = offsets.read()
        for alert in feed.fetch_after(cursor):
            self._sender.send(self._spec.chat, self._spec.render(alert))
            cursor = max(cursor, alert.ident)
            offsets.write(cursor)

    def _offset(self, name: str) -> Path:
        """The per-feed high-water file, keyed by platform name."""
        return self._spec.state / f'donations-{name.lower()}.offset'

    def produce(self, _emit: Emit) -> None:
        """Poll forever; unconfigured, end at once (clean no-op)."""
        if not self._ready():
            _LOG.info('idle: sender, chat or feeds not configured')
            return
        while not self.stopped:
            self.drain_once()
            self.wait(self._spec.poll_sec)

    def _ready(self) -> bool:
        """Whether the sender, chat and at least one live feed are set."""
        return (
            self._sender.live
            and bool(self._spec.chat)
            and any(feed.live for feed in self._feeds)
        )
