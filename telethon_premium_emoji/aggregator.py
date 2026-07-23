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

The post's texts and emoji (author, announce phrases, love/ps/arrow emoji,
platform glyphs, the "view" label) are all editable in
``aggregator_constants.json`` -- a JSON file, so it may hold non-ASCII text.
In-flight groups are persisted to ``aggregator_state.json`` and restored on
start, so a restart within the (2 hour) window does not lose pending videos.

Env: TELEGRAM_API_ID, TELEGRAM_API_HASH, SOURCE_CHAT_ID (where the JSON
arrives), TARGET_CHAT_ID (where to post), and optional PLATFORMS, TITLE_MATCH
(0-1, default 0.9), AGGREGATE_TIMEOUT_SEC (default 7200).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING

from premium_emoji import RichText
from telethon import TelegramClient
from telethon import events

if TYPE_CHECKING:
    from collections.abc import Iterable

    from premium_emoji import PremiumMessage

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
# Files next to this script: the editable constants and the saved state.
CONSTANTS_FILE = 'aggregator_constants.json'
STATE_FILE = 'aggregator_state.json'

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
    backfill: int


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
    created_at: float = field(default_factory=time.time)
    task: asyncio.Task[None] | None = None


# The incoming-message JSON keys, so a typo in the API can be fixed in the
# constants file (the "fields" object) without touching code.
DEFAULT_FIELDS = {
    'caption': 'caption',
    'platform': 'platform',
    'link': 'link',
    'thumbnail': 'thumnailUrl',
    'duration': 'duration',
}


@dataclass(frozen=True)
class Consts:
    """Randomizable texts and emoji for the post, loaded from JSON."""

    fields: dict[str, str]
    author: str
    announce: list[str]
    love: list[object]
    ps: list[object]
    arrow_down: list[object]
    view_label: str
    column_separator: str
    rows: list[list[str]]
    platform_emoji: dict[str, object]


def _load_constants(path: Path) -> Consts:
    """Load the post constants from JSON, ignoring unknown keys."""
    data = json.loads(path.read_text(encoding='utf-8'))
    return Consts(
        fields={**DEFAULT_FIELDS, **(data.get('fields') or {})},
        author=str(data.get('author', '')),
        announce=list(data.get('announce') or ['']),
        love=list(data.get('love') or ['']),
        ps=list(data.get('ps') or ['']),
        arrow_down=list(data.get('arrow_down') or ['']),
        view_label=str(data.get('view_label', 'View')),
        column_separator=str(data.get('column_separator', '  |  ')),
        rows=list(data.get('rows') or []),
        platform_emoji=dict(data.get('platform_emoji') or {}),
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


def _parse_item(
    data: dict[str, object], msg_id: int, fields: dict[str, str]
) -> Item | None:
    """Build an Item from a parsed JSON object, or None if incomplete.

    ``fields`` maps our names to the incoming JSON keys, so a renamed or
    misspelled API key is fixed in the constants file, not here.
    """
    title = str(data.get(fields['caption']) or '').strip()
    platform = str(data.get(fields['platform']) or '').strip()
    if not title or not platform:
        return None
    return Item(
        key=platform.lower(),
        platform=platform,
        title=title,
        url=str(data.get(fields['link']) or '').strip(),
        thumbnail=str(data.get(fields['thumbnail']) or '').strip(),
        duration=str(data.get(fields['duration']) or '').strip(),
        msg_id=msg_id,
    )


def _primary(group: Group, order: Iterable[str]) -> Item:
    """The highest-priority item present; its caption/thumbnail lead."""
    for key in order:
        item = group.items.get(key)
        if item is not None:
            return item
    return next(iter(group.items.values()))


def _group_dict(group: Group) -> dict[str, object]:
    """Serialize a Group to a JSON-friendly dict."""
    return {
        'title': group.title,
        'created_at': group.created_at,
        'msg_ids': sorted(group.msg_ids),
        'items': {key: asdict(item) for key, item in group.items.items()},
    }


def _group_from_dict(raw: dict[str, object]) -> Group:
    """Rebuild a Group from its serialized dict."""
    items = {
        key: Item(**value) for key, value in (raw.get('items') or {}).items()
    }
    return Group(
        title=str(raw.get('title', '')),
        items=items,
        msg_ids=set(raw.get('msg_ids') or []),
        created_at=float(raw.get('created_at') or time.time()),
    )


def _youtube_thumb(group: Group) -> str:
    """The thumbnail URL from the YouTube item only (per the spec), or ''."""
    item = group.items.get('youtube')
    return item.thumbnail if item else ''


def _strip_tags(caption: str) -> str:
    """Caption without its trailing hashtags, for display."""
    return ' '.join(_HASHTAG_RE.sub(' ', caption).split())


def _cells(group: Group, row: list[str]) -> list[str]:
    """Platform keys in a row that have a link, in the row's order."""
    return [p for p in row if group.items.get(p) and group.items[p].url]


def _compose_links(rich: RichText, group: Group, consts: Consts) -> None:
    """Append the platform link grid: '<emoji> View | <emoji> View' rows."""
    for row in consts.rows:
        cells = _cells(group, row)
        for index, key in enumerate(cells):
            if index:
                rich.text(consts.column_separator)
            rich.emoji(consts.platform_emoji.get(key, '')).text(' ')
            rich.link(consts.view_label, group.items[key].url)
        if cells:
            rich.text('\n')


def _compose(
    group: Group, order: tuple[str, ...], consts: Consts
) -> PremiumMessage:
    """Build the full post: author line, description line, and link grid."""
    caption = _strip_tags(_primary(group, order).title)
    rich = RichText()
    rich.text(consts.author).text(' ')
    rich.text(random.choice(consts.announce)).text(' ')  # noqa: S311
    rich.emoji(random.choice(consts.love)).text('\n\n')  # noqa: S311
    rich.emoji(random.choice(consts.ps)).text(' ')  # noqa: S311
    rich.text(caption).text(' ')
    rich.emoji(random.choice(consts.arrow_down)).text('\n\n')  # noqa: S311
    _compose_links(rich, group, consts)
    return rich.build()


class Aggregator:
    """Groups platform messages by title and posts the collected links."""

    def __init__(self, client: TelegramClient, config: Config) -> None:
        here = Path(__file__)
        self.client = client
        self.config = config
        self.consts = _load_constants(here.with_name(CONSTANTS_FILE))
        self.state_path = here.with_name(STATE_FILE)
        self.groups: list[Group] = []
        self.rejected: set[str] = set()
        self.me_id = 0

    async def on_message(self, message: object) -> None:
        """Route one incoming message into its video group."""
        msg_id = int(getattr(message, 'id', 0) or 0)
        if _already_liked(message, self.me_id):
            log.info('msg %s: already processed (reacted), skipping', msg_id)
            return
        item = self._accept(message)
        if item is None:
            return
        group = self._match(item.title) or self._start(item)
        group.items[item.key] = item
        group.msg_ids.add(item.msg_id)
        missing = [p for p in self.config.platforms if p not in group.items]
        log.info(
            'caught msg %s (%s) for %r -- have %d/%d, waiting for: %s',
            item.msg_id,
            item.platform,
            group.title,
            len(group.items),
            len(self.config.platforms),
            ', '.join(missing) or 'nothing, complete',
        )
        self._save()
        if not missing:
            await self._flush(group)

    def _accept(self, message: object) -> Item | None:
        """Parse a message into a Short's item, or None to ignore it."""
        data = _extract_json(getattr(message, 'message', '') or '')
        msg_id = int(getattr(message, 'id', 0) or 0)
        item = _parse_item(data, msg_id, self.consts.fields) if data else None
        if item is None:
            return None
        if _norm(item.title) in self.rejected:
            log.info('msg %s: video already rejected as non-Short', msg_id)
            return None
        seconds = _duration_seconds(item.duration)
        if seconds >= MAX_SHORT_SEC:
            log.info(
                'msg %s: %s is %ss (>= %ss) -- not a Short, dropping %r',
                msg_id,
                item.platform,
                seconds,
                MAX_SHORT_SEC,
                item.title,
            )
            self._reject(item.title)
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
        self._save()

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
        self._arm(group)
        return group

    def _arm(self, group: Group) -> None:
        """Schedule the group's timeout flush."""
        group.task = asyncio.create_task(self._expire(group))

    async def _expire(self, group: Group) -> None:
        """Flush a group once its timeout (from creation) elapses."""
        remaining = self.config.timeout - (time.time() - group.created_at)
        if remaining > 0:
            await asyncio.sleep(remaining)
        log.info('timeout for %r -- posting what arrived', group.title)
        await self._flush(group)

    async def _flush(self, group: Group) -> None:
        """Post the collected links once, mark the sources, then forget it."""
        if group not in self.groups:
            return
        self.groups.remove(group)
        if group.task is not None:
            group.task.cancel()
        log.info(
            'posting %r with %d platform(s): %s',
            group.title,
            len(group.items),
            ', '.join(sorted(group.items)),
        )
        message = _compose(group, self.config.platforms, self.consts)
        await self._post(message, _youtube_thumb(group))
        await self._like(group.msg_ids)
        log.info(
            'posted %r and liked %d source msg(s)',
            group.title,
            len(group.msg_ids),
        )
        self._save()

    async def backfill(self) -> None:
        """Scan recent source history for messages not yet processed."""
        limit = self.config.backfill
        if limit <= 0:
            return
        log.info(
            'backfill: scanning last %d messages of %s ...',
            limit,
            self.config.source,
        )
        try:
            history = await self.client.get_messages(
                self.config.source, limit=limit
            )
        except Exception:  # noqa: BLE001 -- source may be unreachable at start
            log.warning('backfill: could not read source history')
            return
        for message in reversed(history):  # oldest first
            await self.on_message(message)
        log.info('backfill: done (%d messages scanned)', len(history))

    async def _like(self, msg_ids: set[int]) -> None:
        """React to each processed source message, best-effort."""
        for msg_id in msg_ids:
            try:
                await self.client.send_reaction(
                    self.config.source, msg_id, LIKE_REACTION
                )
            except Exception:  # noqa: BLE001 -- reactions may be off in chat
                log.warning('could not react to message %s', msg_id)

    async def _post(self, message: PremiumMessage, thumb: str) -> None:
        """Send the message as a photo (if a thumbnail) or plain text."""
        if thumb:
            try:
                await self.client.send_file(
                    self.config.target,
                    thumb,
                    caption=message.text,
                    formatting_entities=message.entities,
                )
            except Exception:  # noqa: BLE001 -- bad thumb falls back to text
                log.warning('thumbnail send failed; posting as text')
            else:
                return
        await self.client.send_message(
            self.config.target,
            message.text,
            formatting_entities=message.entities,
            link_preview=False,
        )

    def _save(self) -> None:
        """Persist in-flight groups and the rejected set to disk (atomic)."""
        data = {
            'rejected': sorted(self.rejected),
            'groups': [_group_dict(g) for g in self.groups],
        }
        tmp = self.state_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        tmp.replace(self.state_path)

    def restore(self) -> None:
        """Reload saved state and re-arm timers (call once at startup)."""
        if not self.state_path.exists():
            return
        data = json.loads(self.state_path.read_text(encoding='utf-8'))
        self.rejected = set(data.get('rejected') or [])
        for raw in data.get('groups') or []:
            group = _group_from_dict(raw)
            self.groups.append(group)
            self._arm(group)
        log.info('restored %d pending videos from disk', len(self.groups))


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
        # How many recent source messages to scan at startup for unprocessed
        # ones (those without our reaction).
        backfill=int(os.environ.get('BACKFILL_LIMIT', '100')),
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
    agg.restore()
    log.info(
        'Listening on %s; posting to %s; platforms=%s',
        config.source,
        config.target,
        ','.join(config.platforms),
    )
    await agg.backfill()
    await client.run_until_disconnected()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info('Stopped.')
