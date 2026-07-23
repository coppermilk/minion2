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
in the post and picks which platform's caption leads (the thumbnail is taken
from YouTube only). After a video is posted, its source message ids are saved
to disk; on restart, backfill skips any message whose id was already posted, so
re-posting never happens.

Notes:
    * The link is read from ``link`` (or ``url``); the thumbnail from
      ``thumnailUrl`` (the API spelling), then ``thumbnailUrl``/``thumbnail``.
    * ``thumnailUrl`` is optional; when present the post is that photo with
      the links as its caption, otherwise a plain text message.
    * Messages must be valid JSON; anything that does not parse is ignored.

Every behaviour knob is editable in ``aggregator_constants.json`` (a JSON file,
so it may hold non-ASCII text): platforms, title_match, timeout_sec, backfill,
max_duration_sec, the incoming field names, and the post's texts and emoji.
In-flight groups are persisted to ``aggregator_state.json`` and restored on
start, so a restart within the timeout window does not lose pending videos and
never re-posts.

The env holds only the deploy knobs: credentials (TELEGRAM_API_ID,
TELEGRAM_API_HASH, optional TELEGRAM_PASSWORD), the monitoring chat
SOURCE_CHAT_ID, and the target chat(s) TARGET_CHAT_ID (comma-separated -- list
several chats to post to all of them). The chats live ONLY in the env, the
behaviour ONLY in the JSON -- there is no overlap. The session file is
TELEGRAM_SESSION_FILE (default 'telethon.session' next to this package); state
goes to AGGREGATOR_STATE_DIR when set.
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

from telethon import TelegramClient
from telethon import events

from minions.aggregator.premium_emoji import RichText

if TYPE_CHECKING:
    from collections.abc import Iterable

    from minions.aggregator.premium_emoji import PremiumMessage

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
# Files next to this script: the editable constants and the saved state.
CONSTANTS_FILE = 'aggregator_constants.json'
STATE_FILE = 'aggregator_state.json'
# How often to log the pending videos and what each still awaits.
STATUS_INTERVAL = 60
# How many processed source-message ids to remember (restart dedup).
PROCESSED_CAP = 5000
# Chat command (sent in the watcher chat) that previews the premium emoji.
COMMAND_EMOJIS = '/emojis'

_HASHTAG_RE = re.compile(r'#\S+')
_NONWORD_RE = re.compile(r'[^\w\s]')  # drops emoji and punctuation; keeps text


@dataclass(frozen=True)
class Config:
    """Runtime settings for the aggregator, all resolved from the env."""

    source: int
    targets: tuple[int, ...]
    platforms: tuple[str, ...]
    threshold: float
    timeout: float
    backfill: int
    max_duration: int


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
    'action': 'action',
    'caption': 'caption',
    'platform': 'platform',
    'link': 'link',
    'thumbnail': 'thumnailUrl',
    'duration': 'duration',
}
# Thumbnail key spellings seen in the wild; any is accepted (optional field).
_THUMB_ALIASES = ('thumbnail', 'thumbnailUrl', 'thumnailUrl')


@dataclass(frozen=True)
class Consts:
    """Randomizable texts and emoji for the post, loaded from JSON."""

    fields: dict[str, str]
    action_value: str
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
        action_value=str(data.get('action_value', '')),
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
    """Load simple KEY=VALUE lines from a .env file (environment wins)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        os.environ.setdefault(key.strip(), value.strip().strip('\'"'))


# The project keeps ONE .env at the repo root (compose's env_file and the
# Windows launcher both point there). This package is minions/aggregator/, so
# parents[2] is that repo root. In Docker the vars are already in os.environ
# (compose env_file), so a missing file here is harmless; env always wins.
PROJECT_ENV = Path(__file__).resolve().parents[2] / '.env'


def load_env() -> None:
    """Load the project's root .env so a bare run finds the credentials."""
    _load_dotenv(PROJECT_ENV)


# Default file-session base path: 'telethon.session' next to this package.
# It is git-ignored, so a session file kept in the repo checkout survives a
# repo re-sync (deploy/nas-update.sh's `git reset --hard`), exactly like .env.
# Telethon appends '.session', so the file on disk is 'telethon.session'.
DEFAULT_SESSION_PATH = Path(__file__).with_name('telethon')


def _resolve_session_path() -> Path:
    """The file-session base path, from TELEGRAM_SESSION_FILE or the default.

    A trailing '.session' is stripped so the value works whether you point at
    the file itself or its base name (e.g. /data/bots/aggregator/session in the
    container, on the persistent mount).
    """
    override = os.environ.get('TELEGRAM_SESSION_FILE')
    if not override:
        return DEFAULT_SESSION_PATH
    path = Path(override).expanduser()
    if path.suffix == '.session':
        path = path.with_suffix('')
    return path


def _resolve_state_path(default: Path) -> Path:
    """Where the in-flight state file lives: AGGREGATOR_STATE_DIR or default.

    In the container the package dir is ephemeral (/app), so compose points
    AGGREGATOR_STATE_DIR at the /data mount -- then dedup (posted ids) and any
    pending groups survive a `compose down/up` (nas-update). Unset for local
    runs keeps the state next to the package.
    """
    state_dir = os.environ.get('AGGREGATOR_STATE_DIR')
    if not state_dir:
        return default
    path = Path(state_dir).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path / STATE_FILE


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


def _action_ok(data: dict[str, object], consts: Consts) -> bool:
    """Whether the message's action is the one we act on (or no filter set)."""
    if not consts.action_value:
        return True
    value = str(data.get(consts.fields['action']) or '')
    return value == consts.action_value


def _extract_fields(text: str, keys: Iterable[str]) -> dict[str, str]:
    """Pull "key": value pairs from possibly-invalid JSON-ish text.

    The source API is not strict JSON (trailing commas, unquoted or unclosed
    values), so instead of json.loads we find each wanted key and read its
    value: a quoted string, or a bareword up to the next comma or brace.
    """
    value_re = r'"\s*:\s*("(?:[^"\\]|\\.)*"|[^,}\n]*)'
    found: dict[str, str] = {}
    for key in keys:
        match = re.search('"' + re.escape(key) + value_re, text)
        if match is None:
            continue
        value = match.group(1).strip().removeprefix('"').removesuffix('"')
        found[key] = value.replace('\\/', '/').replace('\\"', '"').strip()
    return found


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
        thumbnail=_pick(data, fields['thumbnail'], *_THUMB_ALIASES),
        duration=str(data.get(fields['duration']) or '').strip(),
        msg_id=msg_id,
    )


def _pick(data: dict[str, object], *keys: str) -> str:
    """First non-empty value among ``keys`` (handles optional/renamed keys)."""
    for key in keys:
        value = str(data.get(key) or '').strip()
        if value:
            return value
    return ''


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


def _emoji_id_str(spec: object) -> str:
    """The document id of a premium emoji spec, or '(plain)' for a glyph."""
    if isinstance(spec, dict):
        return str(spec.get('id', '?'))
    return '(plain)'


def _emoji_section(rich: RichText, label: str, specs: list[object]) -> None:
    """Append one labelled block of emoji, each with its id, to ``rich``."""
    rich.text(label + ':\n')
    for spec in specs:
        rich.emoji(spec).text(' ' + _emoji_id_str(spec) + '\n')
    rich.text('\n')


def _render_constants(consts: Consts) -> PremiumMessage:
    """A preview of every premium emoji constant, rendered with its id."""
    rich = RichText()
    rich.text('Premium emoji constants\n\n')
    _emoji_section(rich, 'love', consts.love)
    _emoji_section(rich, 'ps', consts.ps)
    _emoji_section(rich, 'arrow_down', consts.arrow_down)
    rich.text('platforms:\n')
    for name, spec in consts.platform_emoji.items():
        rich.emoji(spec).text(f' {name} {_emoji_id_str(spec)}\n')
    return rich.build()


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
        self.state_path = _resolve_state_path(here.with_name(STATE_FILE))
        keys = [*self.consts.fields.values(), *_THUMB_ALIASES]
        self._keys = tuple(dict.fromkeys(keys))
        self.groups: list[Group] = []
        self.rejected: set[str] = set()
        self.processed_ids: set[int] = set()

    async def on_message(self, message: object) -> None:
        """Route one incoming message into its video group."""
        msg_id = int(getattr(message, 'id', 0) or 0)
        preview = (getattr(message, 'message', '') or '').replace('\n', ' ')
        log.info('received msg %s: %.120s', msg_id, preview)
        if msg_id in self.processed_ids:
            log.info('msg %s: already posted, skipping', msg_id)
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
        msg_id = int(getattr(message, 'id', 0) or 0)
        text = getattr(message, 'message', '') or ''
        data = _extract_fields(text, self._keys)
        if not data:
            log.info('msg %s: no recognizable fields, ignoring', msg_id)
            return None
        if not _action_ok(data, self.consts):
            log.info(
                'msg %s: action is not %r, skipping',
                msg_id,
                self.consts.action_value,
            )
            return None
        item = _parse_item(data, msg_id, self.consts.fields)
        if item is None or _norm(item.title) in self.rejected:
            log.info('msg %s: no platform/caption or already rejected', msg_id)
            return None
        return self._short_or_reject(item, msg_id)

    def _short_or_reject(self, item: Item, msg_id: int) -> Item | None:
        """Return the item if it is a Short, else reject the video and log.

        An empty/absent duration means unknown -- treated as a Short (kept).
        """
        seconds = _duration_seconds(item.duration)
        if seconds >= self.config.max_duration:
            log.info(
                'msg %s: %s is %ss (>= %ss) -- not a Short, dropping %r',
                msg_id,
                item.platform,
                seconds,
                self.config.max_duration,
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
        self._mark_posted(group.msg_ids)
        log.info('posted %r', group.title)
        self._save()

    def _mark_posted(self, msg_ids: set[int]) -> None:
        """Record posted source ids so a restart never re-posts them."""
        self.processed_ids |= msg_ids
        if len(self.processed_ids) > PROCESSED_CAP:
            keep = sorted(self.processed_ids)[-PROCESSED_CAP:]
            self.processed_ids = set(keep)

    async def show_constants(self) -> None:
        """Post a preview of every premium emoji constant to the watcher."""
        message = _render_constants(self.consts)
        await self.client.send_message(
            self.config.source,
            message.text,
            formatting_entities=message.entities,
        )
        log.info('sent premium constants preview to %s', self.config.source)

    async def status_loop(self) -> None:
        """Periodically log which videos are pending and what they await."""
        while True:
            await asyncio.sleep(STATUS_INTERVAL)
            if not self.groups:
                continue
            for group in self.groups:
                missing = [
                    p for p in self.config.platforms if p not in group.items
                ]
                log.info(
                    'pending %r: have [%s], still waiting for [%s]',
                    group.title,
                    ', '.join(sorted(group.items)),
                    ', '.join(missing),
                )

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

    async def _post(self, message: PremiumMessage, thumb: str) -> None:
        """Send the message to every target as a photo or plain text."""
        for target in self.config.targets:
            if thumb:
                try:
                    await self.client.send_file(
                        target,
                        thumb,
                        caption=message.text,
                        formatting_entities=message.entities,
                    )
                except Exception:  # noqa: BLE001 -- bad thumb falls back to text
                    log.warning('thumbnail send failed; posting as text')
                else:
                    continue
            await self.client.send_message(
                target,
                message.text,
                formatting_entities=message.entities,
                link_preview=False,
            )

    def _save(self) -> None:
        """Persist state (groups, rejected, posted ids) to disk (atomic)."""
        data = {
            'rejected': sorted(self.rejected),
            'processed_ids': sorted(self.processed_ids),
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
        self.processed_ids = set(data.get('processed_ids') or [])
        for raw in data.get('groups') or []:
            group = _group_from_dict(raw)
            self.groups.append(group)
            self._arm(group)
        log.info(
            'restored %d pending videos, %d processed ids from disk',
            len(self.groups),
            len(self.processed_ids),
        )


def _source() -> int:
    """The monitoring (source) chat id from the env, else the default."""
    return int(os.environ.get('SOURCE_CHAT_ID') or DEFAULT_SOURCE_CHAT_ID)


def _targets() -> tuple[int, ...]:
    """The target chat ids from the env (comma-separated), else the default.

    Set TARGET_CHAT_ID (or TARGET_CHAT_IDS) to a comma-separated list to post
    the same message to several chats. Chats live in the env only, never in the
    JSON.
    """
    raw = (
        os.environ.get('TARGET_CHAT_IDS')
        or os.environ.get('TARGET_CHAT_ID')
        or str(DEFAULT_TARGET_CHAT_ID)
    )
    return tuple(int(p.strip()) for p in raw.split(',') if p.strip())


def _load_config() -> Config:
    """Chats come from the env; all behaviour from the constants JSON."""
    data = json.loads(
        Path(__file__).with_name(CONSTANTS_FILE).read_text(encoding='utf-8')
    )
    csv = str(data.get('platforms') or DEFAULT_PLATFORMS)
    platforms = tuple(p.strip().lower() for p in csv.split(',') if p.strip())
    return Config(
        source=_source(),
        targets=_targets(),
        platforms=platforms,
        threshold=float(data.get('title_match') or 0.9),
        # Two hours by default: platforms can arrive far apart. The wait is a
        # local timer (asyncio.sleep), so it costs Telegram nothing.
        timeout=float(data.get('timeout_sec') or 7200),
        # Recent source messages to scan at startup for unprocessed ones.
        backfill=int(data.get('backfill') or 100),
        # A video whose known duration reaches this many seconds is dropped.
        max_duration=int(data.get('max_duration_sec') or MAX_SHORT_SEC),
    )


async def main() -> None:
    """Listen to the source chat and aggregate videos across platforms."""
    load_env()

    api_id = os.environ.get('TELEGRAM_API_ID')
    api_hash = os.environ.get('TELEGRAM_API_HASH')
    if not api_id or not api_hash:
        raise SystemExit('Set TELEGRAM_API_ID and TELEGRAM_API_HASH.')
    config = _load_config()

    session_path = _resolve_session_path()
    session_path.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(session_path), int(api_id), api_hash)
    agg = Aggregator(client, config)

    async def _handler(event: events.NewMessage.Event) -> None:
        if (event.raw_text or '').strip().lower() == COMMAND_EMOJIS:
            await agg.show_constants()
            return
        await agg.on_message(event.message)

    client.add_event_handler(_handler, events.NewMessage(chats=config.source))

    # TELEGRAM_PASSWORD supplies the 2FA/cloud password non-interactively;
    # unset, Telethon prompts for it (getpass) only if the account has 2FA.
    start_kwargs: dict[str, object] = {}
    password = os.environ.get('TELEGRAM_PASSWORD')
    if password:
        start_kwargs['password'] = password
    log.info('Session store: %s.session', session_path)
    await client.start(**start_kwargs)
    agg.restore()
    log.info(
        'Listening on %s; posting to %s; platforms=%s',
        config.source,
        ','.join(str(t) for t in config.targets),
        ','.join(config.platforms),
    )
    await agg.backfill()
    status_task = asyncio.create_task(agg.status_loop())
    await client.run_until_disconnected()
    status_task.cancel()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info('Stopped.')
