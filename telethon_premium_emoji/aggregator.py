"""Aggregate the same video across platforms, then post the collected links.

A userbot listens to a source chat where bots (or people) drop one JSON object
per video per platform:

    {"title": "...", "duration": "14:32", "platform": "YouTube",
     "thumbnail": "https://...jpg", "url": "https://..."}

Messages whose titles match ~90% are treated as the same video. Once all the
expected platforms have arrived for a video (or a timeout elapses), one message
collecting every platform's link is posted to the target chat.

Notes / assumptions:

* The per-platform video link is read from ``url`` (or ``link``). The example
  schema only showed ``thumbnail``, so set the real field name if it differs.
* ``thumbnail`` is optional; when present the post is sent as that photo with
  the links as its caption, otherwise as a text message.

Env: TELEGRAM_API_ID, TELEGRAM_API_HASH, SOURCE_CHAT_ID (where the JSON
arrives), TARGET_CHAT_ID (where to post), and optional PLATFORMS,
TITLE_MATCH (0-1, default 0.9), AGGREGATE_TIMEOUT_SEC (default 300).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from dataclasses import field
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING

from telethon import TelegramClient
from telethon import events

if TYPE_CHECKING:
    from collections.abc import Iterable

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(name)s: %(message)s',
)
log = logging.getLogger('aggregator')

DEFAULT_TARGET_CHAT_ID = -1002431466060
DEFAULT_PLATFORMS = 'youtube,tiktok,instagram,pinterest'

# Plain (non-premium) platform glyphs, written as ASCII escapes.
_EMOJI = {
    'youtube': '\U000025b6',
    'tiktok': '\U0001f3b5',
    'instagram': '\U0001f4f7',
    'pinterest': '\U0001f4cc',
}

_JSON_RE = re.compile(r'\{.*\}', re.DOTALL)


@dataclass(frozen=True)
class Config:
    """Runtime settings for the aggregator, all resolved from the env."""

    source: int
    target: int
    platforms: tuple[str, ...]
    threshold: float
    timeout: float


@dataclass(frozen=True)
class Item:
    """One platform's message about a video."""

    key: str  # normalized platform, e.g. 'youtube'
    platform: str  # display name as received
    title: str
    url: str
    thumbnail: str
    duration: str


@dataclass
class Group:
    """A set of platform items believed to be the same video."""

    title: str
    items: dict[str, Item] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None


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


def _norm(title: str) -> str:
    """Normalize a title for fuzzy comparison (lowercase, single spaces)."""
    return ' '.join(title.lower().split())


def _similar(a: str, b: str) -> float:
    """Similarity ratio of two normalized titles, in [0, 1]."""
    return SequenceMatcher(None, a, b).ratio()


def _extract_json(text: str) -> dict[str, object] | None:
    """The first JSON object embedded in a message, or None."""
    match = _JSON_RE.search(text or '')
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _parse_item(data: dict[str, object]) -> Item | None:
    """Build an Item from a parsed JSON object, or None if incomplete."""
    title = str(data.get('title') or '').strip()
    platform = str(data.get('platform') or '').strip()
    if not title or not platform:
        return None
    url = str(data.get('url') or data.get('link') or '').strip()
    return Item(
        key=platform.lower(),
        platform=platform,
        title=title,
        url=url,
        thumbnail=str(data.get('thumbnail') or '').strip(),
        duration=str(data.get('duration') or '').strip(),
    )


def _first_thumb(group: Group) -> str:
    """The first thumbnail URL among the group's items, or ''."""
    for item in group.items.values():
        if item.thumbnail:
            return item.thumbnail
    return ''


def _render(group: Group, order: Iterable[str]) -> str:
    """The collected-links message: title, then one line per platform."""
    lines = [group.title, '']
    for key in order:
        item = group.items.get(key)
        if item and item.url:
            glyph = _EMOJI.get(key, '')
            lines.append(f'{glyph} {item.platform}: {item.url}'.strip())
    duration = next(
        (i.duration for i in group.items.values() if i.duration), ''
    )
    if duration:
        lines.extend(['', f'Duration: {duration}'])
    return '\n'.join(lines)


class Aggregator:
    """Groups platform messages by title and posts the collected links."""

    def __init__(self, client: TelegramClient, config: Config) -> None:
        self.client = client
        self.config = config
        self.groups: list[Group] = []

    async def on_message(self, text: str) -> None:
        """Route one incoming message into its video group."""
        data = _extract_json(text)
        item = _parse_item(data) if data else None
        if item is None:
            return
        group = self._match(item.title) or self._start(item)
        group.items[item.key] = item
        log.info(
            'video %r: have %d/%d platforms',
            group.title,
            len(group.items),
            len(self.config.platforms),
        )
        if all(p in group.items for p in self.config.platforms):
            await self._flush(group)

    def _match(self, title: str) -> Group | None:
        """An existing group whose title is >= threshold similar, or None."""
        norm = _norm(title)
        for group in self.groups:
            if _similar(norm, _norm(group.title)) >= self.config.threshold:
                return group
        return None

    def _start(self, item: Item) -> Group:
        """Create a group for a new video and arm its flush timeout."""
        group = Group(title=item.title)
        self.groups.append(group)
        group.task = asyncio.create_task(self._expire(group))
        return group

    async def _expire(self, group: Group) -> None:
        """Flush a group with whatever arrived once the timeout elapses."""
        await asyncio.sleep(self.config.timeout)
        log.info('timeout for %r -- posting partial', group.title)
        await self._flush(group)

    async def _flush(self, group: Group) -> None:
        """Post the collected links once, then forget the group."""
        if group not in self.groups:
            return
        self.groups.remove(group)
        if group.task is not None:
            group.task.cancel()
        await self._post(
            _render(group, self.config.platforms), _first_thumb(group)
        )

    async def _post(self, text: str, thumb: str) -> None:
        """Send the message as a photo (if a thumbnail) or plain text."""
        if thumb:
            try:
                await self.client.send_file(
                    self.config.target, thumb, caption=text
                )
            except Exception:  # noqa: BLE001 -- bad thumb falls back to text
                log.warning('thumbnail send failed; posting as text')
            else:
                return
        await self.client.send_message(
            self.config.target, text, link_preview=False
        )


def _load_config() -> Config:
    """Read the aggregator settings from the environment."""
    source = os.environ.get('SOURCE_CHAT_ID')
    if not source:
        raise SystemExit('Set SOURCE_CHAT_ID (the chat the JSON arrives in).')
    platforms = tuple(
        p.strip().lower()
        for p in os.environ.get('PLATFORMS', DEFAULT_PLATFORMS).split(',')
        if p.strip()
    )
    return Config(
        source=int(source),
        target=int(os.environ.get('TARGET_CHAT_ID', DEFAULT_TARGET_CHAT_ID)),
        platforms=platforms,
        threshold=float(os.environ.get('TITLE_MATCH', '0.9')),
        timeout=float(os.environ.get('AGGREGATE_TIMEOUT_SEC', '300')),
    )


async def main() -> None:
    """Listen to the source chat and aggregate videos across platforms."""
    _load_dotenv(Path(__file__).with_name('.env'))

    api_id = os.environ.get('TELEGRAM_API_ID')
    api_hash = os.environ.get('TELEGRAM_API_HASH')
    if not api_id or not api_hash:
        raise SystemExit('Set TELEGRAM_API_ID and TELEGRAM_API_HASH.')
    config = _load_config()

    client = TelegramClient('telethon_premium_emoji', int(api_id), api_hash)
    agg = Aggregator(client, config)

    async def _handler(event: events.NewMessage.Event) -> None:
        await agg.on_message(event.raw_text)

    client.add_event_handler(_handler, events.NewMessage(chats=config.source))

    await client.start()
    log.info(
        'Listening on %s; posting to %s; platforms=%s',
        config.source,
        config.target,
        ','.join(config.platforms),
    )
    await client.run_until_disconnected()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info('Stopped.')
