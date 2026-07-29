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
Runtime state is persisted to ``aggregator_state.json`` (human-readable,
indented) and restored on start: ``posted`` (what went out -- title, links,
time -- doubling as the re-post guard), ``pending`` (videos still collecting
platforms) and ``rejected``. A restart within the timeout window loses nothing
and never re-posts.

The env holds only the deploy knobs: credentials (TELEGRAM_API_ID,
TELEGRAM_API_HASH, optional TELEGRAM_PASSWORD), the monitoring chat
SOURCE_CHAT_ID, and the target chat(s) TARGET_CHAT_ID (comma-separated -- list
several chats to post to all of them). The chats live ONLY in the env, the
behaviour ONLY in the JSON -- there is no overlap. The session file and the
state default to <DRIVE>/bots/aggregator/ (DRIVE is the library root: your
Google Drive on Windows, /data in the NAS container) -- so the session is
created and read from the same place on every host. Override with
TELEGRAM_SESSION_FILE / AGGREGATOR_STATE_DIR; with no DRIVE either, they fall
back next to this package.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING

from telethon import TelegramClient
from telethon import events
from telethon.tl.functions.messages import GetDiscussionMessageRequest

from minions.aggregator import cats
from minions.aggregator.premium_emoji import RichText

if TYPE_CHECKING:
    from collections.abc import Iterable

    from minions.aggregator.cats import CatEmoji
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
# How many posted videos to keep in the readable log; this doubles as the
# restart-dedup window (300 videos >> the backfill scan, so no re-posts).
POSTED_CAP = 300
# Chat commands (from ANY chat, ANYONE), always rendered into the source chat:
# /emojis previews the whole unified emoji array (all types) with ids; /preview
# renders sample posts (partial + full platform coverage) for QC; /status
# reports what is pending, what was posted/rejected, the last posts and the cat
# engine's live state; /requeue refreshes the pending-cat queue.
COMMAND_EMOJIS = '/emojis'
COMMAND_PREVIEW = '/preview'
COMMAND_STATUS = '/status'
# /requeue safely refreshes the pending-cat queue: cancel the in-flight timers
# and re-arm from the persisted queue (renewing any that are due).
COMMAND_REQUEUE = '/requeue'
# How many recent messages to scan when checking whether the operator already
# replied to a comment by hand (so the bot does not pile a cat on top).
CAT_REPLY_SCAN = 200
# How many existing comments per watched thread to consider at startup, so
# comments made before the bot started can still get a (delayed) cat.
COMMENT_SCAN = 50

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


@dataclass(frozen=True)
class Posted:
    """A readable record of one video that was posted (state log + dedup)."""

    title: str
    at: str  # ISO 8601 UTC, e.g. '2026-07-23T14:20:00Z'
    links: dict[str, str]  # platform -> url
    msg_ids: list[int]  # the source messages consumed by this post


def _iso(ts: float) -> str:
    """A unix timestamp as an ISO-8601 UTC string (second precision)."""
    return datetime.fromtimestamp(ts, tz=UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def _parse_iso(text: str) -> float:
    """An ISO-8601 UTC string back to a unix timestamp (now on bad input)."""
    try:
        return datetime.fromisoformat(text).timestamp()
    except (ValueError, TypeError):
        return time.time()


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
    lead: list[object]  # random premium emoji that leads the caption line
    arrow_down: list[object]
    view_label: list[str]
    column_separator: str
    rows: list[list[str]]
    platform_emoji: dict[str, object]
    sample_short: str
    sample_long: str
    status_help: str  # the /status legend (expected behaviour), from JSON
    emoji_all: list[
        dict[str, object]
    ]  # unified emoji catalog (new JSON), else []


def _str_list(value: object, default: str) -> list[str]:
    """A list of label strings from a JSON list (or a single string)."""
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value] if value else [default]


def _read_json(path: Path) -> dict[str, object]:
    """Parse the constants JSON; on a bad/missing file, log and use defaults.

    A typo in aggregator_constants.json (e.g. a trailing comma) must not take
    the bot down: log a clear error naming the file and the parse problem, then
    fall back to built-in defaults so the aggregator still starts (posts are
    bland until it is fixed).
    """
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        # A clean one-liner (exc text is in the message) beats a traceback for
        # a config typo, so log.error, not log.exception.
        log.error(  # noqa: TRY400
            '%s is invalid (%s); using defaults -- fix it and restart.',
            path.name,
            exc,
        )
        return {}
    if not isinstance(data, dict):
        log.error('%s must be a JSON object; using defaults.', path.name)
        return {}
    return data


# Every premium emoji lives in ONE top-level "emoji" array in the JSON, each
# entry tagged with its "type"; the post-composition lists (love/lead/arrow/
# platform) and the cat pool are all derived from it, and /emojis renders it in
# this order.
_EMOJI_ORDER = ('love', 'lead', 'arrow', 'platform', 'cat')


def emoji_catalog(data: dict[str, object]) -> list[dict[str, object]]:
    """The unified top-level emoji array (each entry a dict)."""
    raw = data.get('emoji')
    if not isinstance(raw, list):
        return []
    return [dict(e) for e in raw if isinstance(e, dict)]


def emoji_of(catalog: list[dict[str, object]], etype: str) -> list[dict]:
    """Every emoji entry of one ``type`` from the unified catalog."""
    return [e for e in catalog if e.get('type') == etype]


def _load_constants(path: Path) -> Consts:
    """Load the post constants from JSON, ignoring unknown keys."""
    data = _read_json(path)
    samples = dict(data.get('sample_titles') or {})
    catalog = emoji_catalog(data)
    platforms = emoji_of(catalog, 'platform')
    return Consts(
        fields={**DEFAULT_FIELDS, **(data.get('fields') or {})},
        action_value=str(data.get('action_value', '')),
        author=str(data.get('author', '')),
        announce=list(data.get('announce') or ['']),
        love=emoji_of(catalog, 'love') or [''],
        lead=emoji_of(catalog, 'lead') or [''],
        arrow_down=emoji_of(catalog, 'arrow') or [''],
        view_label=_str_list(data.get('view_label'), 'View'),
        column_separator=str(data.get('column_separator', '  |  ')),
        rows=list(data.get('rows') or []),
        platform_emoji={str(e['name']): e for e in platforms if e.get('name')},
        sample_short=str(samples.get('short') or 'Sample short video'),
        sample_long=str(samples.get('long') or 'Sample long video'),
        status_help=str(data.get('status_help') or ''),
        emoji_all=catalog,
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


# Last-resort file-session base path: 'telethon.session' next to this package.
# It is git-ignored, so a session file kept in the repo checkout survives a
# repo re-sync (deploy/nas-update.sh's `git reset --hard`), exactly like .env.
# Telethon appends '.session', so the file on disk is 'telethon.session'.
DEFAULT_SESSION_PATH = Path(__file__).with_name('telethon')


def _drive_dir() -> Path | None:
    """The aggregator data dir <DRIVE>/bots/aggregator, or None if no DRIVE.

    DRIVE is the project's library root -- the Google Drive folder on Windows,
    /data in the NAS container (compose forces it). Every bot keeps its files
    under <DRIVE>/bots/<name>/, so the session and state default there too: on
    Windows that is your Google Drive, on the NAS the /data mount.
    """
    drive = os.environ.get('DRIVE')
    if not drive:
        return None
    return Path(drive).expanduser() / 'bots' / 'aggregator'


def _resolve_session_path() -> Path:
    """The file-session base path (override, else <DRIVE>, else the package).

    A trailing '.session' is stripped so an override works whether you point at
    the file or its base name. With no TELEGRAM_SESSION_FILE, the session lives
    at <DRIVE>/bots/aggregator/telethon.session (DRIVE set), else next to the
    package.
    """
    override = os.environ.get('TELEGRAM_SESSION_FILE')
    if override:
        path = Path(override).expanduser()
        return path.with_suffix('') if path.suffix == '.session' else path
    drive = _drive_dir()
    return drive / 'telethon' if drive is not None else DEFAULT_SESSION_PATH


def _resolve_state_path(default: Path) -> Path:
    """Where the state file lives (override, else <DRIVE>, else the package).

    Same rule as the session: an explicit AGGREGATOR_STATE_DIR wins, else it
    sits under <DRIVE>/bots/aggregator (Windows Google Drive or the NAS /data
    mount, so it survives a `compose down/up`), else next to the package.
    """
    override = os.environ.get('AGGREGATOR_STATE_DIR')
    directory = Path(override) if override else _drive_dir()
    if directory is None:
        return default
    directory = directory.expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / STATE_FILE


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


def _posted_dict(post: Posted) -> dict[str, object]:
    """A Posted record as a readable JSON dict."""
    return {
        'title': post.title,
        'at': post.at,
        'links': post.links,
        'msg_ids': sorted(post.msg_ids),
    }


def _posted_from_dict(raw: dict[str, object]) -> Posted:
    """Rebuild a Posted record from its dict."""
    return Posted(
        title=str(raw.get('title', '')),
        at=str(raw.get('at', '')),
        links=dict(raw.get('links') or {}),
        msg_ids=[int(i) for i in (raw.get('msg_ids') or [])],
    )


def _pending_dict(
    group: Group, platforms: tuple[str, ...]
) -> dict[str, object]:
    """A pending Group as a readable, resumable JSON dict."""
    items = {
        key: {
            'url': item.url,
            'thumbnail': item.thumbnail,
            'duration': item.duration,
            'msg_id': item.msg_id,
        }
        for key, item in group.items.items()
    }
    return {
        'title': group.title,
        'since': _iso(group.created_at),
        'waiting': [p for p in platforms if p not in group.items],
        'items': items,
        'msg_ids': sorted(group.msg_ids),
    }


def _pending_from_dict(raw: dict[str, object]) -> Group:
    """Rebuild a Group from a pending dict (or an old-schema group dict)."""
    title = str(raw.get('title', ''))
    items = {
        key: Item(
            key=key,
            platform=key,
            title=title,
            url=str(value.get('url', '')),
            thumbnail=str(value.get('thumbnail', '')),
            duration=str(value.get('duration', '')),
            msg_id=int(value.get('msg_id', 0)),
        )
        for key, value in (raw.get('items') or {}).items()
    }
    since = raw.get('since')
    created_at = (
        _parse_iso(str(since))
        if since is not None
        else float(raw.get('created_at') or time.time())
    )
    return Group(
        title=title,
        items=items,
        msg_ids=set(raw.get('msg_ids') or []),
        created_at=created_at,
    )


def _youtube_thumb(group: Group) -> str:
    """The thumbnail URL from the YouTube item only (per the spec), or ''."""
    item = group.items.get('youtube')
    return item.thumbnail if item else ''


def _strip_tags(caption: str) -> str:
    """Caption without its trailing hashtags, for display."""
    return ' '.join(_HASHTAG_RE.sub(' ', caption).split())


def _trim(title: str, width: int = 40) -> str:
    """A one-line, length-capped title for the /status report."""
    flat = ' '.join(title.split())
    return flat if len(flat) <= width else flat[: width - 1] + '~'


def _thread_top(reply: object) -> int | None:
    """The thread-root id a reply belongs to (comment target), or None.

    A comment on a channel post is a reply in the discussion group: its
    ``reply_to_top_id`` is the post's thread root; a first-level comment has
    only ``reply_to_msg_id`` (the same root). Either way this yields the id the
    engine watches, so nested and top-level comments both map to their post.
    """
    top = getattr(reply, 'reply_to_top_id', None)
    if top is not None:
        return int(top)
    msg = getattr(reply, 'reply_to_msg_id', None)
    return int(msg) if msg is not None else None


def _cells(group: Group, row: list[str]) -> list[str]:
    """Platform keys in a row that have a link, in the row's order."""
    return [p for p in row if group.items.get(p) and group.items[p].url]


def _grid_cells(group: Group, consts: Consts) -> list[list[tuple[str, str]]]:
    """Pre-pick each cell's (platform key, random label), row by row."""
    return [
        [
            (key, random.choice(consts.view_label))  # noqa: S311
            for key in _cells(group, row)
        ]
        for row in consts.rows
    ]


def _col_widths(rows: list[list[tuple[str, str]]]) -> dict[int, int]:
    """The longest label actually used in each column (to align the '|')."""
    widths: dict[int, int] = {}
    for row in rows:
        for col, (_key, label) in enumerate(row):
            widths[col] = max(widths.get(col, 0), len(label))
    return widths


def _compose_links(rich: RichText, group: Group, consts: Consts) -> None:
    """Append the platform link grid: '<emoji> View | <emoji> View' rows.

    Each label is padded only to the longest label actually used in its
    column, so the '|' separators line up with no wasted space -- and the last
    cell of a row is never padded (no trailing spaces).
    """
    rows = _grid_cells(group, consts)
    widths = _col_widths(rows)
    for row in rows:
        last = len(row) - 1
        for index, (key, label) in enumerate(row):
            rich.emoji(consts.platform_emoji.get(key, '')).text(' ')
            rich.link(label, group.items[key].url)
            if index != last:
                pad = ' ' * (widths[index] - len(label))
                rich.text(pad + consts.column_separator)
        if row:
            rich.text('\n')


def _catalog_suffix(entry: dict[str, object]) -> str:
    """The trailing id + platform name / cat tags for one catalog entry."""
    parts = [str(entry.get('id', '?'))]
    if entry.get('name'):
        parts.append(str(entry['name']))
    tags = entry.get('tags')
    if tags:
        parts.append('[' + ','.join(str(t) for t in tags) + ']')
    return ' '.join(parts)


def _render_constants(consts: Consts) -> PremiumMessage:
    """Render the whole unified emoji array for /emojis, grouped by type."""
    rich = RichText()
    rich.text(f'Premium emoji ({len(consts.emoji_all)})\n\n')
    by_type: dict[str, list[dict[str, object]]] = {}
    for entry in consts.emoji_all:
        by_type.setdefault(str(entry.get('type', 'other')), []).append(entry)
    extra = [t for t in by_type if t not in _EMOJI_ORDER]
    for etype in (*_EMOJI_ORDER, *extra):
        items = by_type.get(etype)
        if not items:
            continue
        rich.text(f'{etype} ({len(items)}):\n')
        for entry in items:
            rich.emoji(entry).text(' ' + _catalog_suffix(entry) + '\n')
        rich.text('\n')
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
    rich.emoji(random.choice(consts.lead)).text(' ')  # noqa: S311
    rich.text(caption).text(' ')
    rich.emoji(random.choice(consts.arrow_down)).text('\n\n')  # noqa: S311
    _compose_links(rich, group, consts)
    return rich.build()


# QC preview (/preview): fake titles + dummy links, one video per scenario.
# The two sample captions live in the constants JSON (so this source stays
# ASCII, BLUEPRINT 4); _sample_groups reads them off the loaded Consts.


def _sample_item(key: str, msg_id: int, title: str) -> Item:
    """One fake platform item for a QC preview post."""
    thumb = 'https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg'
    return Item(
        key=key,
        platform=key,
        title=title,
        url=f'https://example.com/{key}',
        thumbnail=thumb if key == 'youtube' else '',
        duration='0:0:20',
        msg_id=msg_id,
    )


def _sample_group(platforms: Iterable[str], title: str) -> Group:
    """A fake Group covering the given platforms, for a QC preview."""
    group = Group(title=title)
    for msg_id, key in enumerate(platforms):
        group.items[key] = _sample_item(key, msg_id, title)
    return group


def _sample_groups(consts: Consts) -> list[Group]:
    """Five sample videos for QC: from one platform arrived up to all four."""
    short = consts.sample_short
    long = consts.sample_long
    return [
        _sample_group(['youtube'], short),
        _sample_group(['tiktok', 'youtube'], short),
        _sample_group(['instagram', 'pinterest'], short),
        _sample_group(['tiktok', 'youtube', 'instagram'], long),
        _sample_group(['tiktok', 'youtube', 'pinterest', 'instagram'], short),
    ]


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
        # posted is the readable log; processed_ids is its flattened msg-id set
        # (kept for O(1) dedup) and is always rebuilt from posted.
        self.posted: list[Posted] = []
        self.processed_ids: set[int] = set()
        # The human-like cat-reply engine: it tracks the last posts, decides
        # whether/when to reply to a commenter, and picks the cat emoji. Its
        # params/pool come from the constants JSON 'cats' section; idle unless
        # 'enabled'. Fire-later tasks are held so they are not GC'd mid-sleep.
        raw = _read_json(here.with_name(CONSTANTS_FILE))
        self.cats = cats.CatBrain(
            cats.load_cat_params(raw),
            self.state_path.with_name('cats_state.json'),
        )
        self._cat_tasks: set[asyncio.Task[None]] = set()

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
        self._record_posted(group)
        log.info('posted %r', group.title)
        self._save()

    def _record_posted(self, group: Group) -> None:
        """Append a readable posted record; rebuild the dedup id set."""
        links = {
            key: item.url for key, item in group.items.items() if item.url
        }
        self.posted.append(
            Posted(
                title=group.title,
                at=_iso(time.time()),
                links=links,
                msg_ids=sorted(group.msg_ids),
            )
        )
        del self.posted[:-POSTED_CAP]  # keep only the most recent POSTED_CAP
        self.processed_ids = {i for p in self.posted for i in p.msg_ids}

    async def handle(self, event: events.NewMessage.Event) -> None:
        """Dispatch one event: a /command, a comment cat, or aggregation.

        Commands (/emojis, /preview, /status) work from ANY chat and for
        ANYONE and always render into the source chat; the cat engine watches
        replies to our own posts (any chat); aggregation stays source-scoped.
        """
        text = (event.raw_text or '').strip().lower()
        if await self._command(text):
            return
        if self.cats.params.enabled:
            self._maybe_cat(event)
        if event.chat_id == self.config.source:
            await self.on_message(event.message)

    async def _command(self, text: str) -> bool:
        """Run a matching /command, returning True if one handled the text."""
        handlers = {
            COMMAND_EMOJIS: self.show_constants,
            COMMAND_PREVIEW: self.preview_posts,
            COMMAND_STATUS: self.status_report,
            COMMAND_REQUEUE: self.requeue_cats,
        }
        handler = handlers.get(text)
        if handler is None:
            return False
        await handler()
        return True

    def _maybe_cat(self, event: events.NewMessage.Event) -> None:
        """If this message comments on one of our posts, schedule a cat reply.

        A "comment" is a reply whose target is one of the last posts. Each
        commenter is catted at most once PER POST -- a second comment under the
        same post is ignored, but the same person on a different post is
        eligible again. The engine decides whether and when (it may return
        nothing -- skipped, silent day, already catted here).
        """
        reply = getattr(event.message, 'reply_to', None)
        top = _thread_top(reply)
        chat = int(event.chat_id or 0)
        if not self.cats.is_comment(chat, top):
            return
        person = str(getattr(event, 'sender_id', None) or '')
        if not person:
            return
        # Feedback (principle 8): a reply to our freshest post reads as active
        # engagement, so the reaction comes faster.
        engaged = bool(self.cats.posts) and self.cats.posts[-1] == (chat, top)
        self._schedule_comment(
            chat, top, int(event.message.id), person, engaged=engaged
        )

    def _schedule_comment(
        self,
        chat: int,
        root: int,
        comment_id: int,
        person: str,
        *,
        engaged: bool,
    ) -> None:
        """Schedule (and arm) a cat for one commenter under a watched post.

        Once per (post, commenter): the dedup key ties the person to THIS
        post's thread, so re-commenting under the same post gets no second cat,
        but the same person on another post is eligible again. The engine may
        return nothing (skipped, silent day, already catted here).
        """
        key = f'{chat}:{root}:{person}'
        when = self.cats.schedule(key, engaged=engaged)
        if when is None:
            return
        self.cats.add_pending(chat, comment_id, when)
        self._arm_cat(chat, comment_id, when)

    def _arm_cat(self, chat: int, reply_to: int, when: float) -> None:
        """Create the fire-later task for a scheduled (persisted) cat."""
        task = asyncio.create_task(self._cat_later(chat, reply_to, when))
        self._cat_tasks.add(task)
        task.add_done_callback(self._cat_tasks.discard)

    def rearm_cats(self) -> None:
        """Re-arm cats that were scheduled before a restart (survive downtime).

        Any whose time passed while the host was down is renewed to a fresh
        in-window slot by the engine, so a night's worth does not fire at once.
        """
        for chat, reply_to, when in self.cats.rearm():
            self._arm_cat(chat, reply_to, when)

    async def backfill_cat_posts(self) -> None:
        """Seed the cat watch-list from the posts already in each target.

        Without this, cats only watch posts made AFTER the bot starts noting
        them, so posts that predate a deploy/restart are ignored. Here we look
        up the last ``watch_posts`` real posts per target and register them (in
        the channel case, resolving each one's discussion thread), so comments
        on the existing last posts get cats right away.
        """
        if not self.cats.params.enabled:
            return
        for target in self.config.targets:
            await self._seed_target_posts(target)
        log.info('cats: watch-list has %d post(s)', len(self.cats.posts))

    async def _recent_target_posts(self, target: int, want: int) -> object:
        """The last ``want`` posts in a target (channel: any; group: ours)."""
        if self.cats.params.comments_in_discussion:
            return await self.client.get_messages(target, limit=want)
        return await self.client.get_messages(
            target, limit=want, from_user='me'
        )

    async def _seed_target_posts(self, target: int) -> None:
        """Register the last posts of one target into the cat watch-list."""
        want = self.cats.params.watch_posts
        try:
            history = await self._recent_target_posts(target, want)
        except Exception:  # noqa: BLE001 -- unreachable target: skip, no crash
            log.warning('cats: could not read %s post history', target)
            return
        for message in reversed(list(history)):  # oldest first -> newest last
            msg_id = int(getattr(message, 'id', 0) or 0)
            if msg_id:
                await self._watch_post(target, msg_id)

    async def backfill_cat_comments(self) -> None:
        """Schedule cats for comments already sitting under the watched posts.

        Live events only cover comments that arrive WHILE the bot runs, so
        comments made before it started would never get a cat. Here we scan the
        existing comments in each watched thread and schedule the ones not yet
        catted (dedup, skip and the manual-reply check still apply), spread by
        the engine's heavy-tailed spacing so they trickle out, not burst.
        """
        if not self.cats.params.enabled:
            return
        for chat, root in list(self.cats.posts):
            await self._seed_thread_comments(chat, root)
        log.info('cats: %d comment(s) queued', len(self.cats.state.pending))

    async def _seed_thread_comments(self, chat: int, root: int) -> None:
        """Schedule cats for the recent comments in one watched thread."""
        try:
            comments = await self.client.get_messages(
                chat, reply_to=root, limit=COMMENT_SCAN
            )
        except Exception:  # noqa: BLE001 -- no thread/unreachable: skip quietly
            log.warning('cats: could not read comments of %s/%s', chat, root)
            return
        for message in reversed(list(comments)):  # oldest first
            self._schedule_from_message(chat, root, message)

    def _schedule_from_message(
        self, chat: int, root: int, message: object
    ) -> None:
        """Schedule a cat for one existing comment message (skip our own)."""
        if getattr(message, 'out', False):
            return
        person = str(getattr(message, 'sender_id', None) or '')
        comment_id = int(getattr(message, 'id', 0) or 0)
        if person and comment_id:
            self._schedule_comment(
                chat, root, comment_id, person, engaged=False
            )

    async def requeue_cats(self) -> None:
        """Refresh the pending-cat queue on demand (the /requeue command).

        Cancels the in-flight timers and re-arms from the PERSISTED queue,
        recomputing EVERY pending cat's time (so a queue scheduled under stale
        timing is flushed). Nothing is duplicated -- a cat is only forgotten
        once actually sent.
        """
        for task in list(self._cat_tasks):
            task.cancel()
        self._cat_tasks.clear()
        for chat, reply_to, when in self.cats.rearm(renew_all=True):
            self._arm_cat(chat, reply_to, when)
        count = len(self.cats.state.pending)
        await self.client.send_message(
            self.config.source, f'Requeued {count} pending cat(s).'
        )
        log.info('requeued %d pending cats', count)

    async def _cat_later(self, chat: int, reply_to: int, when: float) -> None:
        """Sleep until ``when``, then reply unless already answered by hand.

        A send failure is logged loudly (not swallowed) and the entry is
        dropped so one poison comment cannot wedge the queue; the person stays
        catted, so it is not rescheduled.
        """
        delay = when - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            if not await self._should_skip_cat(chat, reply_to):
                await self._send_cats(chat, reply_to)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                'cat: send failed in %s (reply_to %s)', chat, reply_to
            )
        self.cats.done_pending(chat, reply_to)

    async def _should_skip_cat(self, chat: int, reply_to: int) -> bool:
        """Skip the cat when the operator already replied to the comment."""
        if not self.cats.params.skip_if_manually_replied:
            return False
        replied = await self._human_replied(chat, reply_to)
        if replied:
            log.info('cat: %s already answered by hand, skipping', reply_to)
        return replied

    async def _human_replied(self, chat: int, comment_id: int) -> bool:
        """Whether an outgoing (manual) reply to ``comment_id`` already exists.

        Scans recent history for a message sent by this account that replies to
        the comment. The cat itself has not been sent yet, so any such reply is
        the operator's own -- do not pile a cat on top of it.
        """
        try:
            history = await self.client.get_messages(
                chat, limit=CAT_REPLY_SCAN
            )
        except Exception:  # noqa: BLE001 -- unreachable: fail open, send anyway
            log.warning(
                'cat: could not check a manual reply to %s', comment_id
            )
            return False
        for message in history:
            if not getattr(message, 'out', False):
                continue
            reply = getattr(message, 'reply_to', None)
            if getattr(reply, 'reply_to_msg_id', None) == comment_id:
                return True
        return False

    async def _send_cats(self, chat: int, reply_to: int) -> None:
        """Send the chosen cat(s) as a reply to the commenter."""
        specs = self.cats.emit()
        for index, spec in enumerate(specs):
            if index:  # the rare second cat trails the first (principle 7)
                await asyncio.sleep(self.cats.params.double_gap_sec)
            await self._send_cat(chat, reply_to, spec)
        if specs:
            log.info('cat: replied with %d emoji in %s', len(specs), chat)

    async def _send_cat(
        self, chat: int, reply_to: int, spec: CatEmoji
    ) -> None:
        """Send one premium cat emoji as a reply to the commenter."""
        emoji = {'id': spec.emoji_id, 'fallback': spec.fallback}
        message = RichText().emoji(emoji).build()
        await self.client.send_message(
            chat,
            message.text,
            formatting_entities=message.entities,
            reply_to=reply_to,
        )

    async def status_report(self) -> None:
        """Post the pending/posted/cat diagnostics to the source chat."""
        labels = await self._chat_labels()
        await self.client.send_message(
            self.config.source, self._status_text(labels)
        )
        log.info('sent status report to %s', self.config.source)

    async def _chat_labels(self) -> dict[int, str]:
        """Resolve every chat shown in /status to a readable @name or title."""
        ids = {self.config.source, *self.config.targets}
        ids |= {chat for chat, _ in self.cats.posts}
        return {cid: await self._chat_label(cid) for cid in ids}

    async def _chat_label(self, chat_id: int) -> str:
        """A chat's @username (or "title") for /status, else the raw id."""
        try:
            entity = await self.client.get_entity(chat_id)
        except Exception:  # noqa: BLE001 -- not cached/reachable: show the id
            return str(chat_id)
        username = getattr(entity, 'username', None)
        if username:
            return f'@{username} ({chat_id})'
        title = getattr(entity, 'title', None) or getattr(
            entity, 'first_name', None
        )
        return f'"{title}" ({chat_id})' if title else str(chat_id)

    def _status_text(self, labels: dict[int, str]) -> str:
        """The full status message: routing, pending, posted, cats, legend."""
        parts = [
            'Aggregator status',
            '',
            *self._routing_lines(labels),
            '',
            *self._pending_lines(),
            '',
            *self._posted_lines(),
            '',
            *self._cat_status_lines(labels),
        ]
        if self.consts.status_help:
            parts += ['', self.consts.status_help]
        return '\n'.join(parts)

    def _routing_lines(self, labels: dict[int, str]) -> list[str]:
        """Where the bot reads (source) and posts (targets), by name."""
        source = labels.get(self.config.source, str(self.config.source))
        targets = ', '.join(labels.get(t, str(t)) for t in self.config.targets)
        return [f'source (reads JSON): {source}', f'target (posts): {targets}']

    def _pending_lines(self) -> list[str]:
        """One line per pending video, and which platforms it awaits."""
        lines = [f'Pending videos: {len(self.groups)}']
        for group in self.groups:
            have = ', '.join(sorted(group.items)) or '-'
            missing = (
                ', '.join(
                    p for p in self.config.platforms if p not in group.items
                )
                or 'complete'
            )
            left = int(self.config.timeout - (time.time() - group.created_at))
            lines.append(
                f'  - "{_trim(group.title)}" have [{have}]'
                f' wait [{missing}] ~{left}s'
            )
        return lines

    def _posted_lines(self) -> list[str]:
        """A tail of what went out, plus the rejected (non-Short) count."""
        head = (
            f'Recently posted: {len(self.posted)}'
            f' | rejected (non-Shorts): {len(self.rejected)}'
        )
        lines = [head]
        for post in self.posted[-5:]:
            links = len(post.links)
            lines.append(
                f'  - "{_trim(post.title)}" {post.at} ({links} links)'
            )
        return lines

    def _cat_status_lines(self, labels: dict[int, str]) -> list[str]:
        """The cat engine's live state (empty-ish when it is disabled)."""
        brain = self.cats
        window = f'{brain.params.active_start:g}-{brain.params.active_end:g}h'
        alive = brain.state.alive
        top = sorted(alive, key=lambda h: alive[h], reverse=True)[:6]
        learned = ', '.join(f'{h}h' for h in top) or '(learning)'
        counters = (
            f'  catted={len(brain.state.catted)}'
            f' pending comments={len(brain.state.pending)}'
            f' mood={brain.state.mood:.2f}'
        )
        return [
            'Cat engine:',
            f'  enabled={brain.params.enabled} pool={len(brain.params.pool)}',
            f'  uptime window (prior)={window}',
            f'  learned on-hours=[{learned}]',
            *self._last_posts_lines(labels),
            counters,
            '  (/requeue to refresh the pending queue)',
        ]

    def _last_posts_lines(self, labels: dict[int, str]) -> list[str]:
        """The watched comment chats + post ids, one per line, by name."""
        posts = self.cats.posts
        lines = [f'  watching comments in ({len(posts)}):']
        lines.extend(
            f'    - {labels.get(ch, str(ch))} post {mid}' for ch, mid in posts
        )
        return lines

    async def show_constants(self) -> None:
        """Post a preview of the whole unified emoji array to the watcher."""
        message = _render_constants(self.consts)
        await self.client.send_message(
            self.config.source,
            message.text,
            formatting_entities=message.entities,
        )
        log.info('sent premium constants preview to %s', self.config.source)

    async def preview_posts(self) -> None:
        """Render QC sample posts (partial + full coverage) to the source."""
        groups = _sample_groups(self.consts)
        for group in groups:
            message = _compose(group, self.config.platforms, self.consts)
            await self.client.send_message(
                self.config.source,
                message.text,
                formatting_entities=message.entities,
                link_preview=False,
            )
        log.info(
            'sent %d QC preview posts to %s', len(groups), self.config.source
        )

    async def status_loop(self) -> None:
        """Periodically log pending videos and learn the host's real uptime."""
        while True:
            await asyncio.sleep(STATUS_INTERVAL)
            if self.cats.params.enabled:
                self.cats.mark_alive(time.time())  # learn actual on-hours
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
        """Send to every target; remember each post as a cat-comment target."""
        for target in self.config.targets:
            sent = await self._send_post(target, message, thumb)
            await self._watch_post(target, int(getattr(sent, 'id', 0) or 0))

    async def _watch_post(self, target: int, post_id: int) -> None:
        """Register where comments on this post will appear (the cat target).

        For a channel with a linked discussion (comments_in_discussion), the
        comments live in the discussion group: resolve the post's thread root
        and watch THAT, so cats land only in the channel post's comments. For a
        plain group target, the post message id itself is the comment target.
        """
        if self.cats.params.comments_in_discussion:
            thread = await self._discussion_thread(target, post_id)
            if thread is not None:
                self.cats.note_post(*thread)
                return
        self.cats.note_post(target, post_id)

    async def _discussion_thread(
        self, channel: int, post_id: int
    ) -> tuple[int, int] | None:
        """(discussion_chat_id, thread_root_id) for a channel post, or None."""
        try:
            disc = await self.client(
                GetDiscussionMessageRequest(peer=channel, msg_id=post_id)
            )
        except Exception:  # noqa: BLE001 -- not a channel / no linked group
            log.warning('cats: no discussion thread for post %s', post_id)
            return None
        messages = getattr(disc, 'messages', None) or []
        if not messages:
            return None
        root = messages[0]
        chat_id = int(getattr(root, 'chat_id', 0) or 0)
        root_id = int(getattr(root, 'id', 0) or 0)
        return (chat_id, root_id) if chat_id and root_id else None

    async def _send_post(
        self, target: int, message: PremiumMessage, thumb: str
    ) -> object:
        """Send one post as a photo (thumb) or text; return the message."""
        if thumb:
            try:
                return await self.client.send_file(
                    target,
                    thumb,
                    caption=message.text,
                    formatting_entities=message.entities,
                )
            except Exception:  # noqa: BLE001 -- bad thumb falls back to text
                log.warning('thumbnail send failed; posting as text')
        return await self.client.send_message(
            target,
            message.text,
            formatting_entities=message.entities,
            link_preview=False,
        )

    def _save(self) -> None:
        """Persist state to disk as readable, indented JSON (atomic)."""
        data = {
            'posted': [_posted_dict(p) for p in self.posted],
            'pending': [
                _pending_dict(g, self.config.platforms) for g in self.groups
            ],
            'rejected': sorted(self.rejected),
        }
        tmp = self.state_path.with_suffix('.tmp')
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        tmp.replace(self.state_path)

    def restore(self) -> None:
        """Reload saved state and re-arm timers (call once at startup)."""
        if not self.state_path.exists():
            return
        data = json.loads(self.state_path.read_text(encoding='utf-8'))
        self.rejected = set(data.get('rejected') or [])
        self._restore_posted(data)
        self._restore_pending(data)
        log.info(
            'restored %d pending videos, %d posted (%d dedup ids) from disk',
            len(self.groups),
            len(self.posted),
            len(self.processed_ids),
        )

    def _restore_posted(self, data: dict[str, object]) -> None:
        """Load the posted log; migrate an old processed_ids-only file."""
        self.posted = [_posted_from_dict(p) for p in data.get('posted') or []]
        self.processed_ids = {i for p in self.posted for i in p.msg_ids}
        # Back-compat: an old file has no posted log, only raw processed_ids --
        # seed the dedup set from it so a restart still never re-posts.
        self.processed_ids |= set(data.get('processed_ids') or [])

    def _restore_pending(self, data: dict[str, object]) -> None:
        """Load pending groups (new 'pending' key or old 'groups') + re-arm."""
        raw_groups = data.get('pending') or data.get('groups') or []
        for raw in raw_groups:
            group = _pending_from_dict(raw)
            self.groups.append(group)
            self._arm(group)


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
    data = _read_json(Path(__file__).with_name(CONSTANTS_FILE))
    csv = str(data.get('platforms') or DEFAULT_PLATFORMS)
    platforms = tuple(p.strip().lower() for p in csv.split(',') if p.strip())
    return Config(
        source=_source(),
        targets=_targets(),
        platforms=platforms,
        threshold=float(data.get('title_match') or 0.9),
        # Three hours by default: platforms can arrive far apart. The wait is
        # a local timer (asyncio.sleep), so it costs Telegram nothing.
        timeout=float(data.get('timeout_sec') or 10800),
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

    # Listen everywhere the account can see: the /emojis preview command works
    # from ANY chat and for ANYONE (it renders back into the source chat);
    # aggregation itself stays scoped to the source chat inside agg.handle.
    client.add_event_handler(agg.handle, events.NewMessage())

    # TELEGRAM_PASSWORD supplies the 2FA/cloud password non-interactively;
    # unset, Telethon prompts for it (getpass) only if the account has 2FA.
    start_kwargs: dict[str, object] = {}
    password = os.environ.get('TELEGRAM_PASSWORD')
    if password:
        start_kwargs['password'] = password
    log.info('Session store: %s.session', session_path)
    await client.start(**start_kwargs)
    agg.restore()
    # The cat startup (re-arm + backfills + heartbeat) must NEVER stop the bot
    # from listening: any failure here is logged and swallowed so we still
    # reach run_until_disconnected below.
    try:
        agg.rearm_cats()  # re-arm cats scheduled before a restart (downtime)
        await agg.backfill_cat_posts()  # watch the last posts already there
        await agg.backfill_cat_comments()  # queue comments already there
        if agg.cats.params.enabled:
            agg.cats.mark_alive(time.time())  # a boot is an uptime observation
    except Exception:
        log.exception('cats: startup step failed; listening anyway')
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
