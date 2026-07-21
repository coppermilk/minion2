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
"""The one platform name wired today (the agnostic default)."""


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
    return DeadFeed(platform)


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
    """Where alerts post, how often, and how each is rendered."""

    chat: str
    offset: Path
    render: Callable[[Donation], str]
    poll_sec: float = DEFAULT_POLL_SEC


class DonationAlerts(Source):
    """Poll a donation feed; post each new alert to a chat.

    Platform-agnostic: any ``Feed`` drives it, so Streamlabs today and
    Revolut or another platform tomorrow reuse this loop unchanged. The
    donation-id high-water mark is persisted per post (STATE,
    REQ-DATA-003), so a restart never replays an alert. Unconfigured, it
    ends at once and the bot degrades to a clean no-op (REQ-DEG-001).
    """

    def __init__(self, feed: Feed, sender: Sender, spec: AlertSpec) -> None:
        super().__init__()
        self._feed = feed
        self._sender = sender
        self._spec = spec
        self._offsets = OffsetStore(spec.offset)

    def drain_once(self, cursor: int) -> int:
        """Post every alert past ``cursor``; return the new high-water."""
        for alert in self._feed.fetch_after(cursor):
            self._sender.send(self._spec.chat, self._spec.render(alert))
            cursor = max(cursor, alert.ident)
            self._offsets.write(cursor)
        return cursor

    def produce(self, _emit: Emit) -> None:
        """Poll forever; unconfigured, end at once (clean no-op)."""
        if not self._ready():
            _LOG.info('idle: feed, sender or chat not configured')
            return
        cursor = self._offsets.read()
        while not self.stopped:
            cursor = self.drain_once(cursor)
            self.wait(self._spec.poll_sec)

    def _ready(self) -> bool:
        """Whether the feed, sender and target chat are all set."""
        return self._feed.live and self._sender.live and bool(self._spec.chat)
