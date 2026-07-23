"""Aggregate the same video across platforms, then post the collected links.

A userbot listens to a source chat where bots (or people) drop one JSON object
per video per platform:

    {"platform": "youtube", "caption": "...", "link": "https://...",
     "thumnailUrl": "https://...jpg", "duration": "0:0:16"}

Messages whose captions match ~90% are treated as the same video. Once all the
expected platforms have arrived (or a timeout elapses), one message collecting
every platform's link is posted to the target chat.

Only Shorts are aggregated: a message whose known ``duration`` reaches 3
minutes marks that video as full-length and drops it. Platforms are ranked by
priority (tiktok, youtube, pinterest, instagram): the order sets the link order
in the post and picks which platform's caption and thumbnail lead. After a
video is posted, its source messages are reacted to (a like); a message that
already carries this account's reaction is skipped as already processed.

Notes:
    * The link is read from ``link`` (or ``url``); the thumbnail from
      ``thumnailUrl`` (the API spelling), then ``thumbnailUrl``/``thumbnail``.
    * ``thumnailUrl`` is optional; when present the post is that photo with
      the links as its caption, otherwise a plain text message.
    * Messages must be valid JSON; anything that does not parse is ignored.

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

DEFAULT_SOURCE_CHAT_ID = -1004402620527
DEFAULT_TARGET_CHAT_ID = -1002431466060
# Priority order: it decides the link order in the post and which platform's
# caption/thumbnail leads. tiktok=1, youtube=2, pinterest=3, instagram=4.
DEFAULT_PLATFORMS = 'tiktok,youtube,pinterest,instagram'
# Only Shorts: a video whose known duration reaches this is dropped.
MAX_SHORT_SEC = 180
# Reaction used to mark a source message as processed.
LIKE_REACTION = '\U0001f44d'  # thumbs up

# Plain (non-premium) platform glyphs, written as ASCII escapes.
_EMOJI = {
    'youtube': '\U000025b6',
    'tiktok': '\U0001f3b5',
    'instagram': '\U0001f4f7',
    'pinterest': '\U0001f4cc',
}

_JSON_RE = re.compile(r'\{.*\}', re.DOTALL)
_HASHTAG_RE = re.compile(r'#\S+')
_NONWORD_RE = re.compile(r'[^\w\s]')  # drops emoji and punctuation; keeps text


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
    msg_id: int


@dataclass
class Group:
    """A set of platform items believed to be the same video."""

    title: str
    items: dict[str, Item] = field(default_factory=dict)
    msg_ids: set[int] = field(default_factory=set)
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
    """Caption core for fuzzy matching: no hashtags, emoji, or punctuation.

    The same video carries different hashtag/emoji tails per platform, so we
    compare only the wording. Falls back to the raw text if stripping empties
    it (a caption that is nothing but hashtags/emoji).
    """
    text = _NONWORD_RE.sub(' ', _HASHTAG_RE.sub(' ', title))
    core = ' '.join(text.lower().split())
    return core or ' '.join(title.lower().split())


def _similar(a: str, b: str) -> float:
    """Similarity ratio of two normalized titles, in [0, 1]."""
    return SequenceMatcher(None, a, b).ratio()


def _duration_seconds(text: str) -> int:
    """Parse 'H:M:S' / 'M:S' / 'S' to seconds; -1 if unknown or unparseable."""
    text = text.strip()
    if not text:
        return -1
    try:
        parts = [int(p) for p in text.split(':')]
    except ValueError:
        return -1
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def _already_liked(message: object, me_id: int) -> bool:
    """Whether this account already reacted to the message (processed)."""
    reactions = getattr(message, 'reactions', None)
    recent = getattr(reactions, 'recent_reactions', None) or []
    for reaction in recent:
        peer = getattr(reaction, 'peer_id', None)
        if getattr(peer, 'user_id', None) == me_id:
            return True
    return False


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


def _parse_item(data: dict[str, object], msg_id: int) -> Item | None:
    """Build an Item from a parsed JSON object, or None if incomplete."""
    title = str(data.get('caption') or data.get('title') or '').strip()
    platform = str(data.get('platform') or '').strip()
    if not title or not platform:
        return None
    url = str(data.get('link') or data.get('url') or '').strip()
    thumb = str(
        data.get('thumnailUrl')  # the API's field, spelled as-is (typo)
        or data.get('thumbnailUrl')
        or data.get('thumbnail')
        or ''
    ).strip()
    return Item(
        key=platform.lower(),
        platform=platform,
        title=title,
        url=url,
        thumbnail=thumb,
        duration=str(data.get('duration') or '').strip(),
        msg_id=msg_id,
    )


def _primary(group: Group, order: Iterable[str]) -> Item:
    """The highest-priority item present; its caption/thumbnail lead."""
    for key in order:
        item = group.items.get(key)
        if item is not None:
            return item
    return next(iter(group.items.values()))


def _first_thumb(group: Group, order: Iterable[str]) -> str:
    """The highest-priority thumbnail URL among the items, or ''."""
    for key in order:
        item = group.items.get(key)
        if item and item.thumbnail:
            return item.thumbnail
    return ''


def _render(group: Group, order: tuple[str, ...]) -> str:
    """The collected-links message: caption, then one line per platform."""
    lines = [_primary(group, order).title, '']
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
        self.rejected: set[str] = set()
        self.me_id = 0

    async def on_message(self, message: object) -> None:
        """Route one incoming message into its video group."""
        if _already_liked(message, self.me_id):
            return
        item = self._accept(message)
        if item is None:
            return
        group = self._match(item.title) or self._start(item)
        group.items[item.key] = item
        group.msg_ids.add(item.msg_id)
        log.info(
            'video %r: have %d/%d platforms',
            group.title,
            len(group.items),
            len(self.config.platforms),
        )
        if all(p in group.items for p in self.config.platforms):
            await self._flush(group)

    def _accept(self, message: object) -> Item | None:
        """Parse a message into a Short's item, or None to ignore it."""
        data = _extract_json(getattr(message, 'message', '') or '')
        msg_id = int(getattr(message, 'id', 0) or 0)
        item = _parse_item(data, msg_id) if data else None
        if item is None or _norm(item.title) in self.rejected:
            return None
        if _duration_seconds(item.duration) >= MAX_SHORT_SEC:
            self._reject(item.title)  # a full-length video, not a Short
            return None
        return item

    def _reject(self, title: str) -> None:
        """Remember a non-Short video and drop any group open for it."""
        self.rejected.add(_norm(title))
        group = self._match(title)
        if group is not None and group in self.groups:
            self.groups.remove(group)
            if group.task is not None:
                group.task.cancel()

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
        """Post the collected links once, mark the sources, then forget it."""
        if group not in self.groups:
            return
        self.groups.remove(group)
        if group.task is not None:
            group.task.cancel()
        order = self.config.platforms
        await self._post(_render(group, order), _first_thumb(group, order))
        await self._like(group.msg_ids)

    async def _like(self, msg_ids: set[int]) -> None:
        """React to each processed source message, best-effort."""
        for msg_id in msg_ids:
            try:
                await self.client.send_reaction(
                    self.config.source, msg_id, LIKE_REACTION
                )
            except Exception:  # noqa: BLE001 -- reactions may be off in chat
                log.warning('could not react to message %s', msg_id)

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
    source = os.environ.get('SOURCE_CHAT_ID', str(DEFAULT_SOURCE_CHAT_ID))
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
        # Two hours by default: platforms can arrive far apart. The wait is a
        # local timer (asyncio.sleep), so it costs Telegram nothing.
        timeout=float(os.environ.get('AGGREGATE_TIMEOUT_SEC', '7200')),
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
        await agg.on_message(event.message)

    client.add_event_handler(_handler, events.NewMessage(chats=config.source))

    await client.start()
    agg.me_id = (await client.get_me()).id
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
