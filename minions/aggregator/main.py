# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
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
re-posting never happens. A second guard covers a video the source RE-DELIVERS
under new ids (an upstream re-emit -- common once the chat's auto-delete has
cleared the old messages): a title >= ``title_match`` similar to a recent post
is not posted again. "Recent" is two overlapping windows and either one blocks:
a TIME window (posted within ``repost_guard_sec`` seconds, 0 disables) and a
COUNT window (among the last ``repost_guard_count`` posted videos, 0 disables).
The count window is clock-independent, so once a title goes out the next
``repost_guard_count`` distinct videos must post before that title is eligible
again -- it holds even if the state file was just restored with stale
timestamps, or the source floods faster than the time window expects.

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
import contextlib
import html
import json
import logging
import os
import random
import threading
import time
from dataclasses import replace
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import TYPE_CHECKING

from telethon import TelegramClient
from telethon import events
from telethon import utils
from telethon.tl.functions.messages import GetDiscussionMessageRequest
from telethon.tl.functions.messages import SendMessageRequest
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.functions.stories import GetAllStoriesRequest
from telethon.tl.functions.stories import IncrementStoryViewsRequest
from telethon.tl.functions.stories import ReadStoriesRequest
from telethon.tl.types import InputReplyToMessage
from telethon.tl.types import ReactionCustomEmoji
from telethon.tl.types import ReactionEmoji

from minion_core.adapters import files
from minions.aggregator import cats
from minions.aggregator import comod
from minions.aggregator import greeter
from minions.aggregator import stories
from minions.aggregator import users
from minions.aggregator.matching import _action_ok
from minions.aggregator.matching import _duration_seconds
from minions.aggregator.matching import _extract_fields
from minions.aggregator.matching import _is_recent_repost
from minions.aggregator.matching import _norm
from minions.aggregator.matching import _parse_item
from minions.aggregator.matching import _similar
from minions.aggregator.models import _THUMB_ALIASES
from minions.aggregator.models import DEFAULT_FIELDS
from minions.aggregator.models import Config
from minions.aggregator.models import Consts
from minions.aggregator.models import Group
from minions.aggregator.models import Item
from minions.aggregator.models import Posted
from minions.aggregator.models import _Comment
from minions.aggregator.models import _iso
from minions.aggregator.models import _story_epoch
from minions.aggregator.premium_emoji import RichText
from minions.aggregator.premium_emoji import build_premium_message
from minions.aggregator.render import _compose
from minions.aggregator.render import _emoji_markup
from minions.aggregator.render import _render_constants
from minions.aggregator.render import _sample_groups
from minions.aggregator.render import _youtube_thumb
from minions.aggregator.runtime import _cancel
from minions.aggregator.runtime import _fmt_eta
from minions.aggregator.runtime import _log_handlers
from minions.aggregator.runtime import _touch_health
from minions.aggregator.runtime import _watchdog
from minions.aggregator.statefile import _pending_dict
from minions.aggregator.statefile import _pending_from_dict
from minions.aggregator.statefile import _posted_dict
from minions.aggregator.statefile import _posted_from_dict

if TYPE_CHECKING:

    from minions.aggregator.premium_emoji import PremiumMessage


def _load_runtime() -> dict[str, object]:
    """Return the 'runtime' section of the constants JSON, or {}."""
    data = _read_json(Path(__file__).with_name(CONSTANTS_FILE))
    rt = data.get('runtime')
    return rt if isinstance(rt, dict) else {}


# force=True reconfigures even if an imported library already installed a root
# handler -- which would make a plain basicConfig a no-op and silently drop our
# INFO level (a likely cause of "no logs"). The file handler means the log is
# always readable on disk, regardless of the container log tab.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(name)s: %(message)s',
    handlers=_log_handlers(),
    force=True,
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
# Which profile is active (live/test). Lives in the base state dir, OUTSIDE the
# per-profile state, so we know which profile to load at startup.
MODE_FILE = 'aggregator_mode.json'
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
# /help (and its natural alias /start) print a plain-language command menu --
# the friendly front door for anyone who has never used the bot.
COMMAND_HELP = '/help'
COMMAND_START = '/start'
COMMAND_EMOJIS = '/emojis'
COMMAND_PREVIEW = '/preview'
COMMAND_STATUS = '/status'
# /requeue safely refreshes the pending-cat queue: cancel the in-flight timers
# and re-arm from the persisted queue (renewing any that are due).
COMMAND_REQUEUE = '/requeue'
# /catnow answers EVERY pending commenter immediately (bypass the human-like
# wait) -- an operator override for "reply to everyone now".
COMMAND_CATNOW = '/catnow'
# /greetnow forces the greeter to poll+process now (no waiting for poll_sec) --
# for testing welcome/farewell DMs.
COMMAND_GREETNOW = '/greetnow'
# /users prints the users-DB summary (audience totals, top commenters, recent
# join/leave) when the users database is enabled.
COMMAND_USERS = '/users'
# /stories prints the story-viewer log: how many stories were viewed and whose,
# most recent first (when the story viewer is enabled).
COMMAND_STORIES = '/stories'
# /comod manages the cabinet ("shkaf"): '/comod <nick> <amount>' moves a
# supporter onto a named shelf (a 30-day timer) and posts the rendered cabinet
# photo plus the move-in announcement; '/comod' alone re-posts the cabinet;
# '/comod kick <nick>' evicts by hand. Takes arguments, so it is matched by
# prefix, not by exact text like the others.
COMMAND_COMOD = '/comod'
# /propiska_shkaf_month prints the month's cabinet registry as text: one line
# per resident -- a random premium heart, the nick, and the move-in date.
COMMAND_PROPISKA = '/propiska_shkaf_month'
# /test and /live switch where posts go: /test routes ALL posts to TEST_CHAT_ID
# (a test channel), /live routes them back to the live targets. Persisted, so
# the mode survives a restart.
COMMAND_TEST = '/test'
COMMAND_LIVE = '/live'
# /features lists the toggleable features and whether each is on. Every feature
# also gets a runtime switch pair '/<name>_on' and '/<name>_off' (e.g.
# /stories_off) that flips its enabled flag, persists the choice (it survives a
# restart, overriding the JSON default), and restarts the profile's loops so
# the change takes effect at once. The togglable feature sections, by name:
COMMAND_FEATURES = '/features'
FEATURE_NAMES = ('cats', 'stories', 'users', 'greeter')
FEATURE_ON_SUFFIX = '_on'
FEATURE_OFF_SUFFIX = '_off'
# The persisted runtime overrides (a name -> bool map) live here, in the base
# state dir so both profiles share one choice (like the JSON's single enabled).
FEATURE_OVERRIDES_FILE = 'feature_overrides.json'
# How many recent messages to scan when checking whether the operator already
# replied to a comment by hand (so the bot does not pile a cat on top).
CAT_REPLY_SCAN = 200
# How many existing comments per watched thread to consider at startup, so
# comments made before the bot started can still get a (delayed) cat.
COMMENT_SCAN = 50
# Just before a cat fires we re-scan its post's thread so a fresh comment need
# not wait for the next rescan loop. Throttled per thread to this many seconds
# so a burst of firings costs at most one extra read per thread (flood-safe).
PRE_FIRE_REFRESH_SEC = 45.0
# How many pending cats to list individually in /status (the rest are summed).
STATUS_PENDING_CATS = 12


def _str_list(value: object, default: str) -> list[str]:
    """Return a list of label strings from a JSON list (or a single string)."""
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
    # A clean one-liner (the exc text is in the message) beats a traceback for
    # a config typo, so this logs at error level without the stack; both bad
    # cases (parse failure, not-an-object) share the one error+defaults path,
    # logged AFTER the except so no exception context is attached.
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


# Every premium emoji lives in ONE top-level "emoji" array in the JSON, each
# entry tagged with its "type"; the post-composition lists (love/lead/arrow/
# platform) and the cat pool are all derived from it, and /emojis renders it in
# this order.


def emoji_catalog(data: dict[str, object]) -> list[dict[str, object]]:
    """Return the unified top-level emoji array (each entry a dict)."""
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
        help_text=str(data.get('help') or ''),
        help_hint=str(data.get('help_hint') or 'Unknown command. Try /help'),
        human_words=tuple(
            str(w).lower() for w in (data.get('human_words') or [])
        ),
        status={
            str(k): str(v)
            for k, v in (data.get('status') or {}).items()
            if not str(k).startswith('_')
        },
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
    """Return the aggregator data dir <DRIVE>/bots/aggregator, or None.

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
    """Return the file-session base (override, else <DRIVE>, else package).

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


def _trim(title: str, width: int = 40) -> str:
    """Return a one-line, length-capped title for the /status report."""
    flat = ' '.join(title.split())
    return flat if len(flat) <= width else flat[: width - 1] + '~'


_EMOJI_ROW_LEN = 2
"""A persisted emoji row is an [id, fallback] pair."""


def _pending_markup(entry: dict[str, object]) -> str:
    """Render a pending entry's chosen cat(s) as premium markup, or '?'.

    Reactions scheduled before the emoji were stored show '?' -- a /requeue
    does not re-pick them (the choice is made at schedule time), it only
    re-times what is already there.
    """
    raw = entry.get('emojis')
    rows = raw if isinstance(raw, list) else []
    markup = ''.join(
        _emoji_markup(str(row[0]), str(row[1]))
        for row in rows
        if len(row) == _EMOJI_ROW_LEN
    )
    return markup or '?'


def _pool_markup(pool: tuple[cats.CatEmoji, ...]) -> str:
    """Render a whole emoji pool as premium markup (a preview strip)."""
    return ''.join(_emoji_markup(c.emoji_id, c.fallback) for c in pool) or '-'


# Link markers that mean a comment wants a real reply (ASCII, so inline);
# the business/outreach words and any non-ASCII marks (e.g. a full-width '?')
# live in the constants JSON's "human_words" (BLUEPRINT 4: source stays ASCII).
_LINK_MARKERS = ('http://', 'https://', 't.me/', 'www.')


def _user_label(row: dict[str, object]) -> str:
    """Return a readable handle for a users-DB row: @username/name/id."""
    username = row.get('username')
    if username:
        return f'@{username}'
    name = row.get('first_name')
    if name:
        return str(name)
    return f'id {row.get("user_id", "?")}'


def _needs_human(text: str, words: tuple[str, ...]) -> bool:
    """Whether a comment wants a real reply, not an auto sticker.

    A question, a link, or business/outreach wording is exactly where a canned
    cat STICKER (a message-shaped reply) reads as a non-sequitur. The caller
    uses this to downgrade such comments to a plain REACTION (safe on anything)
    instead. It never blocks the reaction -- it only keeps message-stickers off
    comments a human would actually answer. This is the one light content gate;
    the engine is otherwise content-agnostic. ``words`` (from the JSON) carries
    the business terms and any non-ASCII marks.
    """
    low = text.lower()
    if '?' in text:
        return True
    if any(u in low for u in _LINK_MARKERS):
        return True
    return any(w in low for w in words)


def _thread_top(reply: object) -> int | None:
    """Return the thread-root id a reply belongs to (comment target), or None.

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


class Aggregator:
    """Groups platform messages by title and posts the collected links."""

    def __init__(self, client: TelegramClient, config: Config) -> None:
        """Load the constants and wire the aggregator's state."""
        here = Path(__file__)
        self.client = client
        self.config = config
        self.consts = _load_constants(here.with_name(CONSTANTS_FILE))
        self._raw = _read_json(here.with_name(CONSTANTS_FILE))
        keys = [*self.consts.fields.values(), *_THUMB_ALIASES]
        self._keys = tuple(dict.fromkeys(keys))
        # State is per PROFILE (live vs test): each mode has its OWN channel
        # AND its own state files (cats, greeter, posted, dedup), so a test run
        # never touches live state and any future stateful feature is isolated
        # for free. The active mode is a marker in the base state dir; live
        # uses that dir (unchanged), test a 'test/' subdir under it.
        base_state = _resolve_state_path(here.with_name(STATE_FILE))
        self._state_base = base_state.parent
        self._mode_path = self._state_base / MODE_FILE
        self._overrides_path = self._state_base / FEATURE_OVERRIDES_FILE
        self._overrides = self._load_overrides()
        self._cat_tasks: set[asyncio.Task[None]] = set()
        self._greeter_task: asyncio.Task[None] | None = None
        self._cat_rescan_task: asyncio.Task[None] | None = None
        self._cat_next_rescan: float = 0.0  # ts of the next auto-rescan
        self._rescan_sec: float = 300.0  # per-profile, set in _build_profile
        self._enrich_tasks: set[asyncio.Task[None]] = set()
        self._story_tasks: set[asyncio.Task[None]] = set()
        self._stories_task: asyncio.Task[None] | None = None
        self._story_next_poll: float = 0.0  # ts of the next stories re-poll
        # Planned-but-not-yet-fired story views, for the /status queue readout.
        self._pending_views: list[stories.StoryView] = []
        # pre-fire thread refresh debounce, keyed by thread root
        self._thread_rescan_at: dict[int, float] = {}
        # ts of the last GetDiscussionMessageRequest, for the flood throttle
        self._last_discussion_ts: float = 0.0
        rt = self._raw.get('runtime')
        rt = rt if isinstance(rt, dict) else {}
        self._probe_timeout = float(rt.get('probe_timeout_sec', 30.0))
        self._build_profile(self._load_mode())

    def _build_profile(self, mode: str) -> None:
        """(Re)bind every mode-scoped part -- channel + state files -- to MODE.

        Live and test are full profiles: each has its own destination channel
        and its own cats/greeter/posted state on disk, so switching is a total,
        automatic sandbox. Resets the in-memory containers; ``start_profile``
        then hydrates them from THIS profile's files.
        """
        self.mode = mode
        pdir = self._profile_dir(mode)
        pdir.mkdir(parents=True, exist_ok=True)
        self.state_path = pdir / STATE_FILE
        self.groups: list[Group] = []
        self.rejected: set[str] = set()
        self.posted: list[Posted] = []
        self.processed_ids: set[int] = set()
        self._cat_tasks = set()
        self._cat_next_rescan = 0.0
        self._rescan_sec = self._rescan_interval(mode)
        # Each feature's on/off is the JSON default unless a /<name>_on|off
        # runtime override is set (``_feature_enabled``), so a toggle survives
        # a restart. The params are frozen, so the flag is swapped via replace.
        cat_params = replace(
            cats.load_cat_params(self._raw),
            enabled=self._feature_enabled('cats'),
        )
        self.cats = cats.CatBrain(cat_params, pdir / 'cats_state.json')
        # Story viewer: watches friends'/contacts' stories the way a person
        # does -- a glance now and then, no reactions -- with its own
        # per-profile seen set and view log. Poll cadence follows the profile.
        story_params = replace(
            stories.load_story_params(self._raw, mode),
            enabled=self._feature_enabled('stories'),
        )
        self.stories = stories.StoryBrain(
            story_params, pdir / 'stories_state.json'
        )
        self._story_next_poll = 0.0
        self._pending_views = []
        # Users DB: its own SQLite file per profile, so live and test audiences
        # never mix. Config lives in the 'users' JSON section.
        ucfg = self._raw.get('users')
        ucfg = ucfg if isinstance(ucfg, dict) else {}
        self._users_enabled = self._feature_enabled('users')
        self._users_store_text = bool(ucfg.get('store_message_text', True))
        self._users_enrich = bool(ucfg.get('enrich', True))
        self.users = users.UserStore(pdir / 'users.db')
        gchannel = self._profile_channel(mode)
        greeter_params = replace(
            greeter.load_greeter_params(self._raw, gchannel),
            enabled=self._feature_enabled('greeter'),
        )
        self.greeter = greeter.Greeter(
            self.client,
            greeter_params,
            greeter.GreeterIO(
                pdir / 'greeter_state.json',
                self._on_membership_event,
            ),
        )
        # The cabinet ("shkaf"): a per-profile shelf roster with a 7-day timer,
        # plus its render/announcement config. Its rendered image is written
        # next to the roster so live and test never share a file.
        self.comod = comod.CabinetRoster(pdir / 'comod.json')
        self._comod = comod.load_comod_params(self._raw)

    def _profile_dir(self, mode: str) -> Path:
        """Return the state dir for MODE: base for live, base/test for test."""
        test = self._state_base / 'test'
        return test if mode == 'test' else self._state_base

    def _rescan_interval(self, mode: str) -> float:
        """Return the auto-rescan period for MODE: test is fast, live is slow.

        Test wants a tight loop while you iterate (default 5 min); live can be
        relaxed (default 1 hour). Both fall back to ``rescan_sec``.
        """
        cfg = self._raw.get('cats')
        cfg = cfg if isinstance(cfg, dict) else {}
        default = float(cfg.get('rescan_sec', 300.0))
        key = 'rescan_sec_test' if mode == 'test' else 'rescan_sec_live'
        return float(cfg.get(key, default))

    def _profile_channel(self, mode: str) -> int:
        """Return the greeter's default channel for MODE (test = test chat)."""
        if mode == 'test':
            return self.config.test_target or self.config.source
        return self.config.targets[0] if self.config.targets else 0

    def _load_mode(self) -> str:
        """Return the active profile ('live' or 'test'), default 'live'."""
        try:
            raw = json.loads(self._mode_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return 'live'
        return 'test' if str(raw.get('mode')) == 'test' else 'live'

    def _save_mode(self) -> None:
        """Persist the active profile so a restart resumes the same mode."""
        self._mode_path.write_text(
            json.dumps({'mode': self.mode}), encoding='utf-8'
        )

    def _load_overrides(self) -> dict[str, bool]:
        """Load the persisted feature on/off overrides (empty if none)."""
        try:
            raw = json.loads(self._overrides_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return {}
        return {
            str(k): bool(v)
            for k, v in raw.items()
            if str(k) in FEATURE_NAMES
        }

    def _save_overrides(self) -> None:
        """Persist the feature overrides so a toggle survives a restart."""
        self._overrides_path.write_text(
            json.dumps(self._overrides, indent=2), encoding='utf-8'
        )

    def _feature_default(self, name: str) -> bool:
        """Return a feature's JSON default ``enabled`` (its section flag)."""
        section = self._raw.get(name)
        section = section if isinstance(section, dict) else {}
        return bool(section.get('enabled', False))

    def _feature_enabled(self, name: str) -> bool:
        """Return a feature's effective on/off: override else JSON default."""
        return self._overrides.get(name, self._feature_default(name))

    async def start_profile(self, *, source_backfill: bool = True) -> None:
        """Hydrate the active profile and start its background loops.

        ``source_backfill`` re-scans the source for missed videos -- wanted at
        boot, but skipped on a live<->test switch so entering test never dumps
        a burst of recent videos into the test channel.
        """
        self.restore()
        try:
            self.rearm_cats()
            await self.backfill_cat_posts()
            await self.backfill_cat_comments()
            if self.cats.params.enabled:
                self.cats.mark_alive(time.time())
        except Exception:
            log.exception('cats: startup step failed; listening anyway')
        if source_backfill:
            await self.backfill()
        self._greeter_task = asyncio.create_task(self.greeter.loop())
        self._cat_rescan_task = asyncio.create_task(self.cat_rescan_loop())
        self._stories_task = asyncio.create_task(self.stories_loop())

    async def stop_profile(self) -> None:
        """Cancel the active profile's timers and loops (before a switch)."""
        self._cancel_cat_tasks()
        self._cancel_story_tasks()
        self._cancel_enrich_tasks()
        for group in self.groups:
            _cancel(getattr(group, 'task', None))
        _cancel(self._greeter_task)
        _cancel(self._cat_rescan_task)
        _cancel(self._stories_task)
        self._greeter_task = None
        self._cat_rescan_task = None
        self._stories_task = None
        self.users.close()  # release the SQLite handle before a rebind

    def _cancel_enrich_tasks(self) -> None:
        """Cancel any in-flight identity-enrichment lookups."""
        for task in list(self._enrich_tasks):
            task.cancel()
        self._enrich_tasks.clear()

    def _cancel_story_tasks(self) -> None:
        """Cancel every in-flight story-view timer (before a mode switch)."""
        for task in list(self._story_tasks):
            task.cancel()
        self._story_tasks.clear()
        self._pending_views.clear()

    async def switch_mode(self, mode: str) -> None:
        """Switch the WHOLE bot to MODE (the /test and /live commands).

        Total sandbox: tear the current profile down, rebind channel + state
        files to MODE, hydrate it. Posts, cats, greeter and any future feature
        all follow -- isolated -- with nothing carried across. Persisted, so a
        restart comes up in the same mode.
        """
        if mode != self.mode:
            await self.stop_profile()
            self._build_profile(mode)
            self._save_mode()
            await self.start_profile(source_backfill=False)
        labels = await self._chat_labels()
        dest = ', '.join(labels.get(t, str(t)) for t in self.live_targets())
        await self.client.send_message(
            self.config.source,
            f'Mode: {self.mode.upper()}. ALL posts now go to: {dest}',
        )
        log.info('mode -> %s, posting to %s', self.mode, self.live_targets())

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
        group = self._group_for(item)
        if group is None:
            return
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
        """Return a group whose title is >= threshold similar, or None."""
        norm = _norm(title)
        for group in self.groups:
            if _similar(norm, _norm(group.title)) >= self.config.threshold:
                return group
        return None

    def _group_for(self, item: Item) -> Group | None:
        """Return the group this item joins, or None to skip it (dup).

        Joins an in-flight group whose title matches; otherwise starts a new
        one -- unless this video was already posted inside the re-post window,
        in which case it is skipped so the same video is not posted twice.
        """
        group = self._match(item.title)
        if group is not None:
            return group
        if self._recently_posted(item.title):
            log.info(
                'msg %s: %r already posted recently, not re-posting',
                item.msg_id,
                item.title,
            )
            return None
        return self._start(item)

    def _recently_posted(self, title: str) -> bool:
        """Whether this video was already posted inside the re-post window.

        The per-message-id guard cannot catch a video the source re-delivers
        under NEW ids (an upstream re-emit, common once the chat's auto-delete
        clears the old messages); this title guard does. It fires on a match
        in EITHER window: within ``repost_guard`` seconds, or among the last
        ``repost_guard_count`` posted videos. Only consulted when no in-flight
        group matches, so platforms of a video still being collected are never
        blocked.
        """
        return _is_recent_repost(
            self.posted,
            title,
            time.time(),
            threshold=self.config.threshold,
            window=self.config.repost_guard,
            count=self.config.repost_guard_count,
        )

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
        """Post the collected links once, mark the sources, then forget it.

        Ordering is deliberate and durable: we RECORD the post and SAVE state
        the instant the message is delivered, BEFORE the ancillary react/watch
        steps. Those steps do slow, flood-prone Telegram calls; if one of them
        raises or the process is restarted mid-way (common), the message is
        already out. Recording after them -- the old order -- left the post
        unrecorded and the pending group still on disk, so the next restart
        re-flushed it and re-posted the same video again and again. If nothing
        is delivered (flood wait, network), the group is re-queued for a later
        retry instead of being dropped.
        """
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
        posts = await self._deliver(message, _youtube_thumb(group))
        if not posts:
            log.warning('post for %r did not go out; re-queueing', group.title)
            group.created_at = time.time()  # a fresh timeout, not a tight loop
            self.groups.append(group)
            self._arm(group)
            self._save()
            return
        self._record_posted(group)  # commit BEFORE react/watch (see docstring)
        log.info('posted %r', group.title)
        self._save()
        for target, post_id in posts:
            await self._react_to_post(target, post_id)
            await self._watch_post(target, post_id)

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
        if await self._unknown_command(event, text):
            return
        self._record_user_message(event)
        if self.cats.params.enabled:
            self._maybe_cat(event)
        if event.chat_id == self.config.source:
            await self.on_message(event.message)

    def _record_user_message(self, event: events.NewMessage.Event) -> None:
        """Log a seen audience message to the users DB (a discussion comment).

        Records non-own messages in the source chat or a watched discussion
        group (the chats the account actually sees), bumping the sender's count
        and storing the text (unless store_message_text is off), then enriches
        the sender's identity lazily. Idempotent per (chat, msg_id).
        """
        message = event.message
        if not self._users_enabled or getattr(message, 'out', False):
            return
        uid = int(getattr(event, 'sender_id', 0) or 0)
        chat = int(event.chat_id or 0)
        disc_chats = {c for c, _ in self.cats.posts}
        if uid <= 0 or (chat != self.config.source and chat not in disc_chats):
            return
        root = _thread_top(getattr(message, 'reply_to', None)) or 0
        body = str(getattr(message, 'message', '') or '')
        self.users.record_message(
            users.SeenMessage(
                uid,
                chat,
                int(getattr(message, 'id', 0) or 0),
                root=int(root),
                text=body if self._users_store_text else '',
            )
        )
        self._maybe_enrich(uid)

    def _on_membership_event(self, event: tuple[int, int, bool, bool]) -> None:
        """Greeter sink: persist a join/leave to the users DB (idempotent)."""
        admin_log_id, user_id, joined, left = event
        if not self._users_enabled or user_id <= 0:
            return
        self.users.record_membership(
            users.MembershipEvent(
                user_id, joined=joined, left=left, admin_log_id=admin_log_id
            )
        )
        self._maybe_enrich(user_id)

    def _maybe_enrich(self, user_id: int) -> None:
        """Schedule a one-off identity lookup for a user we do not know yet."""
        if (
            not self._users_enrich
            or user_id <= 0
            or self.users.has_identity(user_id)
        ):
            return
        task = asyncio.create_task(self._enrich_user(user_id))
        self._enrich_tasks.add(task)
        task.add_done_callback(self._enrich_tasks.discard)

    async def _enrich_user(self, user_id: int) -> None:
        """Resolve a user's username/name (phone is almost always absent)."""
        try:
            entity = await self.client.get_entity(user_id)
        except Exception:  # noqa: BLE001 -- unresolvable id: leave it bare
            return
        self.users.apply_identity(
            users.Identity(
                user_id,
                username=getattr(entity, 'username', None),
                first_name=getattr(entity, 'first_name', None),
                last_name=getattr(entity, 'last_name', None),
                phone=getattr(entity, 'phone', None),
            )
        )

    async def _unknown_command(
        self, event: events.NewMessage.Event, text: str
    ) -> bool:
        """In the source chat, nudge a lone unknown /command toward /help."""
        if event.chat_id != self.config.source:
            return False
        if not text.startswith('/') or ' ' in text or not text[1:].isalpha():
            return False
        await self.client.send_message(
            self.config.source, self.consts.help_hint
        )
        return True

    async def _command(self, text: str) -> bool:
        """Run a matching /command, returning True if one handled the text."""
        # /comod carries arguments ('/comod <nick> <amount>'), so it is matched
        # by its leading word rather than by the exact-text table below.
        if text.split()[:1] == [COMMAND_COMOD]:
            await self.cabinet_command(text)
            return True
        # Feature switches: /features and every '/<name>_on|off'. Matched here,
        # before the exact table, because the name is dynamic (one pair per
        # feature) rather than a fixed command string.
        if await self._feature_command(text):
            return True
        handlers = {
            COMMAND_HELP: self.help_report,
            COMMAND_START: self.help_report,
            COMMAND_EMOJIS: self.show_constants,
            COMMAND_PREVIEW: self.preview_posts,
            COMMAND_STATUS: self.status_report,
            COMMAND_REQUEUE: self.requeue_cats,
            COMMAND_CATNOW: self.answer_all_now,
            COMMAND_GREETNOW: self.greet_now,
            COMMAND_USERS: self.users_report,
            COMMAND_STORIES: self.stories_report,
            COMMAND_PROPISKA: self.propiska_report,
            COMMAND_TEST: self.enter_test,
            COMMAND_LIVE: self.enter_live,
        }
        handler = handlers.get(text)
        if handler is None:
            return False
        await handler()
        return True

    async def _feature_command(self, text: str) -> bool:
        """Handle /features and any '/<name>_on|off', else return False.

        A toggle flips the feature's persisted override and restarts the
        profile so the change takes effect at once. An unknown feature name is
        left unhandled (so it falls through to the /help nudge).
        """
        parts = text.split(maxsplit=1)
        word = parts[0] if parts else ''
        if word == COMMAND_FEATURES:
            await self.features_report()
            return True
        for suffix, on in (
            (FEATURE_ON_SUFFIX, True),
            (FEATURE_OFF_SUFFIX, False),
        ):
            if word.endswith(suffix):
                name = word[1:-len(suffix)]  # strip '/' and the suffix
                if name in FEATURE_NAMES:
                    await self.switch_feature(name, on=on)
                    return True
        return False

    async def switch_feature(self, name: str, *, on: bool) -> None:
        """Turn a feature on/off at runtime (persisted), restarting its loops.

        No-op-reports when already in the wanted state; otherwise records the
        override, then tears the profile down and rebuilds it in the same mode
        so the feature's background loops actually start or stop.
        """
        if self._feature_enabled(name) == on:
            state = 'on' if on else 'off'
            await self.client.send_message(
                self.config.source, f'{name}: already {state}'
            )
            return
        self._overrides[name] = on
        self._save_overrides()
        await self.stop_profile()
        self._build_profile(self.mode)
        await self.start_profile(source_backfill=False)
        state = 'on' if on else 'off'
        log.info('feature %s switched %s', name, state)
        await self.client.send_message(
            self.config.source, f'{name}: switched {state}'
        )

    async def features_report(self) -> None:
        """Post each feature's on/off state and its switch commands."""
        lines = ['Features']
        for name in FEATURE_NAMES:
            dot = self._dot(on=self._feature_enabled(name))
            cmds = f'/{name}_on  /{name}_off'
            lines.append(f'{self._bul()} {dot} {name}  {cmds}')
        await self.client.send_message(
            self.config.source, '\n'.join(lines)
        )
        log.info('sent features report to %s', self.config.source)

    def _maybe_cat(self, event: events.NewMessage.Event) -> None:
        """If this message comments on one of our posts, schedule a cat react.

        A "comment" is a reply whose target is one of the last posts. Each
        commenter is catted at most once PER POST -- a second comment under the
        same post is ignored, but the same person on a different post is
        eligible again. The engine decides whether and when (it may return
        nothing -- skipped, silent day, already catted here).
        """
        if getattr(event.message, 'out', False):
            return  # our own message (a post) -- never cat it
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
        text = _trim(str(getattr(event.message, 'message', '') or ''))
        ref = _Comment(
            chat=chat, root=top, msg_id=int(event.message.id), text=text
        )
        self._schedule_comment(ref, person, engaged=engaged)

    def _schedule_comment(
        self, comment: _Comment, person: str, *, engaged: bool
    ) -> None:
        """Schedule (and arm) a cat for one commenter under a watched post.

        Once per (post, commenter): the dedup key ties the person to THIS
        post's thread, so re-commenting under the same post gets no second cat,
        but the same person on another post is eligible again. The engine may
        return nothing (skipped, silent day, already catted here).

        The cat(s) are CHOSEN here, at schedule time, and stored on the pending
        entry -- so /status and /requeue can show exactly which cat will land
        on which comment, and the send places that same cat rather than a fresh
        random one.
        """
        # When liking everything, key per COMMENT (chat:root:person:msg) so a
        # person's every comment is liked; otherwise once per (post, person).
        # The key keeps the 'chat:root:' prefix so note_post's pruning holds.
        like_all = self.cats.params.like_all
        key = f'{comment.chat}:{comment.root}:{person}'
        if like_all:
            key = f'{key}:{comment.msg_id}'
        when = self.cats.schedule(key, engaged=engaged)
        if when is None:
            return
        # Default is a like REACTION; now and then (deterministic gate) it is a
        # thread STICKER instead -- a premium cat emoji sent as a message. The
        # emoji is pseudo-random but deterministic in the comment id, so the
        # same comment always resolves to the same thing after a restart.
        seed = f'{comment.chat}:{comment.msg_id}'
        post_key = f'{comment.chat}:{comment.root}'
        # A thread STICKER is a message-shaped reply, so it only fits plain
        # enthusiasm; on a question / link / business comment it reads as a
        # non-sequitur. Check content FIRST (so a suppressed sticker does not
        # consume the burst gate), and downgrade to a safe REACTION there.
        allow_sticker = not _needs_human(comment.text, self.consts.human_words)
        sticker = (
            not like_all
            and allow_sticker
            and self.cats.should_sticker(post_key)
        )
        if sticker:
            specs, kind = self.cats.pick_cat(seed), 'reply'
        else:  # like_all always places a like reaction (never a sticker)
            specs, kind = self.cats.pick_like(seed), 'react'
        if not specs:  # empty pool -> nothing to place
            return
        cat = cats.Cat(
            chat=comment.chat,
            reply_to=comment.msg_id,
            root=comment.root,
            when=when,
            text=comment.text,
            emojis=tuple((s.emoji_id, s.fallback) for s in specs),
            kind=kind,
        )
        self.cats.add_pending(cat)
        self._arm_cat(cat)

    def _arm_cat(self, cat: cats.Cat) -> None:
        """Create the fire-later task for a scheduled (persisted) cat."""
        task = asyncio.create_task(self._cat_later(cat))
        self._cat_tasks.add(task)
        task.add_done_callback(self._cat_tasks.discard)

    def rearm_cats(self) -> None:
        """Re-arm cats that were scheduled before a restart (survive downtime).

        Any whose time passed while the host was down is renewed to a fresh
        in-window slot by the engine, so a night's worth does not fire at once.
        """
        for cat in self.cats.rearm():
            self._arm_cat(cat)

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
        for target in self.live_targets():  # test mode -> the test channel
            await self._seed_target_posts(target)
        log.info('cats: watch-list has %d post(s)', len(self.cats.posts))

    async def _recent_target_posts(self, target: int, want: int) -> object:
        """Return the last ``want`` posts in a target (channel/group)."""
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

    async def cat_rescan_loop(self) -> None:
        """Periodically re-scan targets so new posts are picked up by itself.

        A post created (and commented on) while the bot runs is not
        auto-watched by the event stream; without this the operator had to run
        /requeue by hand. Every ``rescan_sec`` this re-seeds the watch-list
        from each target's recent posts and schedules cats for new comments
        (dedup skips what is already queued/answered). ``_cat_next_rescan`` is
        published for the /status countdown. Off when rescan_sec <= 0.
        """
        period = self._rescan_sec
        if not self.cats.params.enabled or period <= 0:
            return
        while True:
            self._cat_next_rescan = time.time() + period
            await asyncio.sleep(period)
            try:
                await self.backfill_cat_posts()
                await self.backfill_cat_comments()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('cats: periodic rescan failed; will retry')

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

    async def stories_loop(self) -> None:
        """Periodically poll the stories feed and view a human-like handful.

        Telegram's own stories feed already limits this to contacts / people
        we follow, so a poll only ever sees friends' stories. Each pass fetches
        the feed, keeps the peers with UNSEEN stories, and lets the brain plan
        a small, human-paced session; each planned view runs on its own timer
        (``_view_later``). No reactions are ever sent -- just a view and a log.
        Off when disabled or the poll period is <= 0.
        """
        period = self.stories.params.poll_sec
        if not self.stories.params.enabled or period <= 0:
            return
        while True:
            self._story_next_poll = time.time() + period
            try:
                await self._poll_stories_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('stories: poll failed; will retry')
            await asyncio.sleep(period)

    async def _poll_stories_once(self) -> None:
        """Fetch the feed, plan a session, and arm a timer per planned view."""
        candidates = await self._fetch_story_candidates()
        if not candidates:
            log.info('stories: no peers with unseen stories this poll')
            return
        views = self.stories.plan(candidates)
        if not views:
            reason = self.stories.blocked_reason() or 'all skipped this glance'
            log.info(
                'stories: %d peer(s) with unseen stories, queued 0 (%s)',
                len(candidates),
                reason,
            )
            return
        for view in views:
            self._pending_views.append(view)  # shown as the /status queue
            task = asyncio.create_task(self._view_later(view))
            self._story_tasks.add(task)
            task.add_done_callback(self._story_tasks.discard)
        log.info(
            'stories: %d peer(s) with unseen stories, queued %d',
            len(candidates),
            len(views),
        )

    async def _fetch_story_candidates(self) -> list[stories.StoryCandidate]:
        """Return the feed's peers that still have unseen stories.

        Reads Telegram's active-stories feed (contacts / followed peers only),
        turns each peer into a ``StoryCandidate`` and keeps just those with at
        least one story id past our persisted seen set -- so we pick up what is
        genuinely new instead of walking the whole contact list. When
        ``include_archived`` is on, the hidden feed (people whose chats were
        moved to the Archive) is polled too and merged in, deduped by peer.
        """
        out: dict[int, stories.StoryCandidate] = {}
        await self._collect_story_feed(out, hidden=False)
        if self.stories.params.include_archived:
            await self._collect_story_feed(out, hidden=True)
        return list(out.values())

    async def _collect_story_feed(
        self, out: dict[int, stories.StoryCandidate], *, hidden: bool
    ) -> None:
        """Read one stories feed (main or hidden) into ``out``, keyed by peer.

        ``hidden`` selects the archived-contacts feed. Unseen-only; a peer
        already collected from the other feed is not overwritten.
        """
        which = 'hidden' if hidden else 'main'
        try:
            res = await self.client(GetAllStoriesRequest(hidden=hidden))
        except Exception:  # noqa: BLE001 -- feed unreachable: skip this pass
            log.warning('stories: could not read the %s stories feed', which)
            return
        feed = getattr(res, 'peer_stories', None) or []
        added = 0
        for peer_stories in feed:
            cand = self._story_candidate(peer_stories)
            if (
                cand is not None
                and cand.peer_id not in out
                and self.stories.unseen(cand)
            ):
                out[cand.peer_id] = cand
                added += 1
        log.info(
            'stories: %s feed has %d peer(s) with stories, %d new-to-us',
            which,
            len(feed),
            added,
        )

    def _story_candidate(
        self, peer_stories: object
    ) -> stories.StoryCandidate | None:
        """Build a ``StoryCandidate`` from one feed entry, or None if empty."""
        peer = getattr(peer_stories, 'peer', None)
        if peer is None:
            return None
        items = getattr(peer_stories, 'stories', None) or []
        ids = [int(getattr(s, 'id', 0) or 0) for s in items]
        ids = [sid for sid in ids if sid > 0]
        if not ids:
            return None
        dates = [_story_epoch(getattr(s, 'date', None)) for s in items]
        return stories.StoryCandidate(
            peer_id=int(utils.get_peer_id(peer)),
            story_ids=tuple(ids),
            max_id=max(ids),
            last_ts=max(dates, default=0.0),
            label=str(utils.get_peer_id(peer)),
        )

    async def _view_later(self, view: stories.StoryView) -> None:
        """Sleep until the view is due, then open the stories and mark them.

        Failures are logged (not swallowed) and the peer is NOT marked seen, so
        a failed read is retried on the next poll rather than silently skipped.
        Every successful view is recorded to the persisted view log with a
        readable @name resolved here (the plan only carries the peer id).
        """
        delay = view.when - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        self._dequeue_view(view)  # it is firing now: drop it from the queue
        try:
            await self._view_stories(view)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('stories: view failed for %s', view.peer_id)
            return
        label = await self._chat_label(view.peer_id)
        self.stories.mark_viewed(view.peer_id, view.story_ids, label=label)
        log.info(
            'stories: viewed %d of %s', len(view.story_ids), label
        )

    def _dequeue_view(self, view: stories.StoryView) -> None:
        """Drop a fired view from the /status queue (no-op if already gone)."""
        with contextlib.suppress(ValueError):
            self._pending_views.remove(view)

    async def _view_stories(self, view: stories.StoryView) -> None:
        """Open each unseen story (human dwell), then mark the peer read.

        Opens the stories one at a time with a short random dwell between them
        (a person does not blink through a whole set instantly), incrementing
        each story's view counter, then marks the peer read up to ``max_id`` --
        the authoritative "seen" signal. Never sends a reaction.
        """
        peer = await self.client.get_input_entity(view.peer_id)
        params = self.stories.params
        for sid in view.story_ids:
            await asyncio.sleep(
                random.uniform(  # noqa: S311 -- human dwell, not crypto
                    params.dwell_min_sec, params.dwell_max_sec
                )
            )
            try:
                await self.client(
                    IncrementStoryViewsRequest(peer=peer, id=[sid])
                )
            except Exception:  # noqa: BLE001 -- best-effort; ReadStories marks
                log.debug('stories: increment view failed for %s', sid)
        await self.client(ReadStoriesRequest(peer=peer, max_id=view.max_id))

    def _schedule_from_message(
        self, chat: int, root: int, message: object
    ) -> None:
        """Schedule a cat for one existing comment message (skip our own)."""
        if getattr(message, 'out', False):
            return
        person = str(getattr(message, 'sender_id', None) or '')
        comment_id = int(getattr(message, 'id', 0) or 0)
        if person and comment_id:
            text = _trim(str(getattr(message, 'message', '') or ''))
            ref = _Comment(chat=chat, root=root, msg_id=comment_id, text=text)
            self._schedule_comment(ref, person, engaged=False)

    async def requeue_cats(self) -> None:
        """Rescan + refresh the pending-cat queue on demand (the /requeue cmd).

        First re-times and re-arms the PERSISTED queue (so a queue scheduled
        under stale timing is flushed). Then it RESCANS the targets: it
        re-seeds the watch-list from each target's recent posts and schedules
        cats for comments not yet queued -- so a post created (and commented
        on) WHILE the bot was running, which is not auto-watched, is picked up
        here instead of returning "nothing queued". Dedup, skip and the
        manual-reply check still apply, so nothing is duplicated.
        """
        self._cancel_cat_tasks()
        for cat in self.cats.rearm(renew_all=True):  # re-time existing queue
            self._arm_cat(cat)
        await self.backfill_cat_posts()  # pick up posts made since startup
        await self.backfill_cat_comments()  # queue their new comments (armed)
        count = len(self.cats.state.pending)
        await self._send_status(await self._plan_text(f'Requeued {count}'))
        log.info('requeued %d pending cats', count)

    async def answer_all_now(self) -> None:
        """Answer EVERY pending commenter immediately (the /catnow command).

        The human-like wait is bypassed: all pending cats are set to fire now
        (the manual-reply check still applies). An operator override. The reply
        lists exactly which cat lands on which comment, so it is never a
        mystery which reaction /catnow placed.
        """
        self._cancel_cat_tasks()
        due = self.cats.due_now()
        for cat in due:
            self._arm_cat(cat)
        await self._send_status(await self._plan_text(f'Answering {len(due)}'))
        log.info('answering %d pending cats now', len(due))

    async def _plan_text(self, head: str) -> str:
        """`<head> pending cat(s):` then a which-cat-where line for each.

        This is what makes /requeue and /catnow legible: every queued reaction
        is listed with the exact cat, the comment, its post, and the eta -- so
        the operator sees the plan instead of a count.
        """
        pending = self.cats.state.pending
        if not pending:
            return f'{head} pending cat(s). Nothing queued.'
        now = time.time()
        lines = [f'{head} pending cat(s):']
        lines.extend(
            self._pending_cat_line(entry, now)
            for entry in pending[:STATUS_PENDING_CATS]
        )
        extra = len(pending) - STATUS_PENDING_CATS
        if extra > 0:
            lines.append(f'    ... (+{extra} more)')
        return '\n'.join(lines)

    async def greet_now(self) -> None:
        """Force the greeter to poll+process now (the /greetnow command)."""
        summary = await self.greeter.sync_now()
        await self.client.send_message(self.config.source, summary)
        log.info('greetnow: %s', summary)

    async def cabinet_command(self, text: str) -> None:
        """Move a nick into the cabinet, evict one, or re-post the cabinet.

        ``/comod <nick> <amount>`` seats NICK on a shelf (refreshing the 30-day
        timer) and posts the rendered cabinet photo with the announcement;
        ``/comod kick <nick>`` evicts by hand and re-posts; a bare ``/comod``
        just re-posts the cabinet. Expired nicks are pruned by the roster on
        every write and read, so a shelf frees up ("s'ekhal") a month after
        its move-in with no extra step. The month's roster of who lives where
        is a separate command (``/propiska_shkaf_month``).
        """
        args = text.split()[1:]
        now = time.time()
        if args and args[0].lower() == 'kick':
            target = args[1].lstrip('@') if len(args) > 1 else ''
            if not target:
                hint = str(self._comod.templates.get('kick_hint', ''))
                await self.client.send_message(self._comod_chat(), hint)
                return
            self.comod.remove(target)
            log.info('comod: evicted %s', target)
        elif args:
            moved_in = args[0]
            amount = args[1] if len(args) > 1 else ''
            self.comod.add(moved_in, amount, now)
            log.info('comod: moved in %s (%s)', moved_in, amount or '-')
        await self._post_cabinet(now)

    async def _post_cabinet(self, now: float) -> None:
        """Render and post the cabinet; text fallback on a render failure.

        Always posts to the source chat (where the command was issued). When
        there are more active residents than shelves, only the TOP donors by
        amount are drawn on the picture.
        """
        active = self.comod.active(now)
        residents = comod.by_amount(active)[: self._comod.max_shelves]
        caption = self._cabinet_caption(residents)
        chat = self._comod_chat()
        image = self._render_cabinet(residents)
        n = len(residents)
        if image is not None:
            try:
                await self.client.send_file(
                    chat, str(image), caption=caption, parse_mode='html'
                )
            except Exception:  # noqa: BLE001 -- bad render falls back to text
                log.warning('comod: image send failed; posting as text')
            else:
                log.info('comod: posted cabinet (%d in) to %s', n, chat)
                return
        await self.client.send_message(
            chat, caption, parse_mode='html', link_preview=False
        )
        log.info('comod: posted cabinet text (%d in) to %s', n, chat)

    def _comod_chat(self) -> object:
        """Where the cabinet posts: the source chat, always (for now)."""
        return self.config.source

    def _render_cabinet(self, residents: list[tuple[str, str]]) -> Path | None:
        """Return the rendered cabinet image, or None if it cannot be made.

        None whenever no template photo is configured (or is missing) or the
        draw fails -- the caller then posts a plain-text roster instead.
        """
        template = self._comod_asset(self._comod.template_path)
        if template is None or not template.is_file():
            return None
        out = self.state_path.parent / 'comod_render.jpg'
        try:
            return files.render_cabinet(
                template,
                out,
                files.CabinetSpec(
                    # Biggest amount on the biggest shelf (area-ranked).
                    comod.assign_labels(residents, self._comod.slots),
                    list(self._comod.slots),
                    font_path=self._comod_font(self._comod.font_path),
                    cyrillic_font_path=self._comod_font(
                        self._comod.font_cyrillic_path
                    ),
                    ref_size=self._comod.ref_size,
                    base_size=self._comod.base_size,
                    amount_scale=self._comod.amount_scale,
                    text_color=self._comod.text_color,
                    shadow_color=self._comod.shadow_color,
                ),
            )
        except Exception:  # noqa: BLE001 -- any Pillow failure -> text roster
            log.warning('comod: render failed for %s', template)
            return None

    def _comod_asset(self, rel: str) -> Path | None:
        """Resolve a comod asset path; a relative one sits in this package.

        So 'assets/cabinet.jpg' and 'assets/fonts/Aleo.ttf' are found no matter
        the working directory. Returns None for a blank path.
        """
        if not rel:
            return None
        path = Path(rel)
        return path if path.is_absolute() else Path(__file__).parent / rel

    def _comod_font(self, rel: str) -> str:
        """Return a bundled font path as a string, or '' when unset/missing.

        Empty lets ``render_cabinet`` fall back to its system-font search.
        """
        path = self._comod_asset(rel)
        return str(path) if path is not None and path.is_file() else ''

    def _cabinet_caption(self, residents: list[tuple[str, str]]) -> str:
        """Return the photo caption: the announcement, or the empty note.

        Only the announcement (with its premium emoji and donation link); who
        lives on which shelf is shown on the picture, and the month's roster is
        the separate /propiska command.
        """
        tpl = self._comod.templates
        if not residents:
            return str(tpl.get('empty', ''))
        return comod.move_in_text(
            tpl,
            '',
            {
                'link': self._comod.donate_link,
                'amazon': self._comod.amazon_link,
            },
        )

    async def propiska_report(self) -> None:
        """Post the month's cabinet registry as text (/propiska_shkaf_month).

        One line per resident -- a random premium heart, the nick, and the
        move-in date -- sent as HTML so the hearts render as premium emoji.
        """
        tpl = self._comod.templates
        entries = self.comod.entries(time.time())
        chat = self._comod_chat()
        if not entries:
            await self.client.send_message(
                chat, str(tpl.get('propiska_empty', '')), parse_mode='html'
            )
            return
        line = str(tpl.get('propiska_line', '{heart} {nick} {date}'))
        rows = [
            line.format(
                heart=self._heart_html(),
                nick=html.escape(nick),
                date=self._move_in_date(at),
            )
            for nick, _amount, at in entries
        ]
        head = str(tpl.get('propiska_head', ''))
        body = '\n'.join(rows)
        text = f'{head}\n{body}' if head else body
        await self.client.send_message(
            chat, text, parse_mode='html', link_preview=False
        )
        log.info('comod: posted propiska (%d) to %s', len(entries), chat)

    def _heart_html(self) -> str:
        """Return a random heart: a premium <tg-emoji>, or its plain glyph."""
        hearts = self._comod.hearts
        if not hearts:
            return ''
        emoji_id, fallback = random.choice(hearts)  # noqa: S311 -- decoration
        if emoji_id:
            return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
        return fallback

    def _move_in_date(self, at: float) -> str:
        """Format a move-in epoch as a date in the persona's timezone."""
        tz = timezone(timedelta(hours=self._comod.tz_offset))
        return datetime.fromtimestamp(at, tz=tz).strftime('%d.%m.%Y')

    async def users_report(self) -> None:
        """Post the users-DB summary to the source chat (/users command)."""
        await self.client.send_message(self.config.source, self._users_text())
        log.info('sent users report to %s', self.config.source)

    def _users_text(self) -> str:
        """Return the /users message: totals, top commenters, join/leave."""
        if not self._users_enabled:
            return 'Users DB: disabled (set users.enabled in the JSON).'
        s = self.users.summary()
        lines = [
            'Users DB',
            (
                f'  total={s["total"]} subscribed={s["subscribed"]}'
                f' left={s["left"]} messages={s["messages"]}'
            ),
        ]
        top = self.users.top_commenters(5)
        if top:
            lines.append('  top commenters:')
            lines += [
                f'    - {_user_label(r)}: {r["msg_count"]} msg' for r in top
            ]
        recent = self.users.recent_events(5)
        if recent:
            lines.append('  recent join/leave:')
            lines += [
                f'    - {r["event"]}: {_user_label(r)}'
                f' {_iso(float(str(r["ts"])))}'
                for r in recent
            ]
        return '\n'.join(lines)

    async def stories_report(self) -> None:
        """Post the story-viewer log to the source chat (/stories command)."""
        await self.client.send_message(
            self.config.source, self._stories_text()
        )
        log.info('sent stories report to %s', self.config.source)

    def _stories_text(self) -> str:
        """Return the /stories message: total viewed and the recent views."""
        if not self.stories.params.enabled:
            return 'Story viewer: disabled (set stories.enabled in the JSON).'
        lines = [f'Story viewer: {self.stories.seen_count()} viewed all-time']
        recent = self.stories.recent_log(10)
        if recent:
            lines.append('  recent views:')
            lines += [
                f'    - {e.get("label") or e.get("peer_id")}:'
                f' {e.get("count")} story(s)'
                f' {_iso(float(str(e.get("ts", 0))))}'
                for e in recent
            ]
        else:
            lines.append('  (nothing viewed yet)')
        return '\n'.join(lines)

    def _stories_lines(self, labels: dict[int, str]) -> list[str]:
        """Return the story-viewer section: header plus the view queue."""
        if not self.stories.params.enabled:
            return [
                self._head('stories', 'Stories', f'{self._dot(on=False)} off')
            ]
        return [self._stories_line(), *self._stories_queue_lines(labels)]

    def _stories_line(self) -> str:
        """Return the story-viewer header: on, count, next view, next poll."""
        now = time.time()
        parts = [
            f'{self._dot(on=True)} on',
            f'{self.stories.seen_count()} viewed',
            f'{len(self._pending_views)} queued',
        ]
        whens = [v.when for v in self._pending_views]
        if whens:
            eta = min(whens) - now
            when = 'now' if eta <= 0 else f'in {_fmt_eta(eta)}'
            parts.append(f'next view {self._arr()} {when}')
        else:
            # Empty queue: say WHY (asleep, cooldown, silent day) so it is not
            # a mystery -- the same reason the poll logs.
            reason = self.stories.blocked_reason(now)
            if reason:
                parts.append(f'idle ({reason})')
        nxt = self._story_next_poll
        poll_eta = nxt - now if nxt else 0.0
        if poll_eta > 0:
            parts.append(f'next poll {self._arr()} in {_fmt_eta(poll_eta)}')
        return self._head('stories', 'Stories', *parts)

    def _stories_queue_lines(self, labels: dict[int, str]) -> list[str]:
        """Return the queued story views: whose, how many, and the ETA."""
        if not self._pending_views:
            return []
        now = time.time()
        b = self._bul()
        views = sorted(self._pending_views, key=lambda v: v.when)
        lines = [f'{b} queued:']
        for view in views[:STATUS_PENDING_CATS]:
            who = labels.get(view.peer_id, str(view.peer_id))
            eta = view.when - now
            when = 'due now' if eta <= 0 else f'in ~{_fmt_eta(eta)}'
            lines.append(
                f'    {who} {b} {len(view.story_ids)} story(s) {b} {when}'
            )
        extra = len(views) - STATUS_PENDING_CATS
        if extra > 0:
            lines.append(f'    ... (+{extra} more)')
        return lines

    def _users_line(self) -> str:
        """Return a one-line users summary for /status ('off' if disabled)."""
        if not self._users_enabled:
            return self._head(
                'users', 'Users DB', f'{self._dot(on=False)} off'
            )
        s = self.users.summary()
        return self._head(
            'users',
            'Users DB',
            f'{self._dot(on=True)} on',
            f'{s["total"]} users',
            f'{s["subscribed"]} subscribed',
            f'{s["messages"]} msgs',
        )

    def _cancel_cat_tasks(self) -> None:
        """Cancel every in-flight fire-later cat task."""
        for task in list(self._cat_tasks):
            task.cancel()
        self._cat_tasks.clear()

    async def _refresh_before_fire(self, cat: cats.Cat) -> None:
        """Pull new comments in this post's thread just before we like it.

        A comment made between rescan loops would otherwise wait for the next
        session; re-reading the thread here queues it now. Debounced per thread
        (``PRE_FIRE_REFRESH_SEC``) so a burst of due cats reads it once.
        """
        if not self.cats.params.enabled:
            return
        root = cat.root or cat.reply_to
        now = time.time()
        if now - self._thread_rescan_at.get(root, 0.0) < PRE_FIRE_REFRESH_SEC:
            return
        self._thread_rescan_at[root] = now
        try:
            await self._seed_thread_comments(cat.chat, root)
        except Exception:  # noqa: BLE001 -- best effort; the cat still fires
            log.warning('cat: pre-fire refresh failed for thread %s', root)

    async def _cat_later(self, cat: cats.Cat) -> None:
        """Sleep until the cat is due, then react unless answered by hand.

        A send failure is logged loudly (not swallowed) and the entry is
        dropped so one poison comment cannot wedge the queue; the person stays
        catted, so it is not rescheduled.
        """
        delay = cat.when - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        await self._refresh_before_fire(cat)
        try:
            if not await self._should_skip_cat(cat.chat, cat.reply_to):
                await self._deliver(cat)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                'cat: react failed in %s (comment %s)', cat.chat, cat.reply_to
            )
        self.cats.done_pending(cat.chat, cat.reply_to)

    async def _should_skip_cat(self, chat: int, comment_id: int) -> bool:
        """Skip the cat when the operator already answered the comment by hand.

        "By hand" is either a manual reply to the comment OR a manual reaction
        already sitting on it -- in both cases the operator has engaged, so the
        bot does not pile a cat reaction on top.
        """
        if not self.cats.params.skip_if_manually_replied:
            return False
        answered = await self._human_answered(chat, comment_id)
        if answered:
            log.info('cat: %s already answered by hand, skipping', comment_id)
        return answered

    async def _human_answered(self, chat: int, comment_id: int) -> bool:
        """Whether the operator already answered ``comment_id`` by hand.

        Two hand signals count, either one wins: an outgoing (manual) reply to
        the comment, or this account's own reaction already sitting on it. The
        cat has not been placed yet, so any such reply/reaction is the
        operator's own -- do not pile a cat reaction on top of it.
        """
        try:
            history = await self.client.get_messages(
                chat, limit=CAT_REPLY_SCAN
            )
        except Exception:  # noqa: BLE001 -- unreachable: fail open, react anyway
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
        return await self._own_reaction(chat, comment_id)

    async def _own_reaction(self, chat: int, comment_id: int) -> bool:
        """Whether this account's own reaction already sits on the comment.

        The reliable signal is the reaction TALLY: Telegram sets
        ``chosen_order`` on every ``results`` entry the current account
        picked, so a non-None chosen_order means "we already reacted here" no
        matter how many others reacted after (unlike ``recent_reactions``,
        which is a short, capacity-capped list). We check the tally first and
        fall back to ``recent_reactions[].my`` for older layers. Best-effort
        and fail-open: an unreadable comment reacts anyway rather than wedging
        the queue.
        """
        try:
            message = await self.client.get_messages(chat, ids=comment_id)
        except Exception:  # noqa: BLE001 -- unreachable: fail open, react anyway
            return False
        reactions = getattr(message, 'reactions', None)
        results = getattr(reactions, 'results', None) or []
        if any(getattr(r, 'chosen_order', None) is not None for r in results):
            return True
        recent = getattr(reactions, 'recent_reactions', None) or []
        return any(getattr(r, 'my', False) for r in recent)

    async def _deliver(self, cat: cats.Cat) -> None:
        """Place the scheduled cat: a like REACTION, or a thread STICKER.

        ``cat.kind`` was decided at schedule time and stored, so the delivery
        is exactly what /status showed: 'react' puts a like reaction ON the
        comment; 'reply' sends the chosen premium cat emoji as a message in the
        comment's thread (it reads like a sticker).
        """
        if cat.kind == 'reply':
            await self._send_sticker(cat)
        else:
            await self._send_cats(cat)

    async def _send_cats(self, cat: cats.Cat) -> None:
        """React to the commenter's message with the cat(s) chosen at schedule.

        The reaction is placed ON the comment itself -- the cat emoji shows as
        a reaction pill under the commenter's message, not as a reply in the
        thread. The emoji were picked when the comment was scheduled and stored
        on ``cat``, so what lands is exactly what /status showed.
        """
        placed = await self._react(cat.chat, cat.reply_to, cat.emojis)
        if placed:
            glyphs = ''.join(fb for _, fb in cat.emojis)
            log.info(
                'cat: reacted %s on comment %s in %s',
                glyphs,
                cat.reply_to,
                cat.chat,
            )

    async def _send_sticker(self, cat: cats.Cat) -> None:
        """Reply IN THE THREAD with the chosen premium cat emoji (a sticker).

        The emoji is sent as a message that replies to the comment inside its
        discussion thread (top=root), so it lands in the post's comments and
        reads like a sticker -- not a reaction pill. Falls back to a flat reply
        if the threaded send is refused (or it is a plain group), so a sticker
        is never lost to threading.
        """
        if not cat.emojis:
            return
        emoji_id, fallback = cat.emojis[0]
        spec = {'id': emoji_id, 'fallback': fallback}
        message = RichText().emoji(spec).build()
        threaded = bool(cat.root) and cat.root != cat.reply_to
        if threaded:
            try:
                await self._reply_in_thread(cat, message)
            except Exception:  # noqa: BLE001 -- fall back to a flat reply
                log.warning(
                    'cat: threaded sticker failed in %s; flat', cat.chat
                )
            else:
                log.info(
                    'cat: sticker %s in thread %s of %s',
                    fallback,
                    cat.root,
                    cat.chat,
                )
                return
        await self.client.send_message(
            cat.chat,
            message.text,
            formatting_entities=message.entities,
            reply_to=cat.reply_to,
            link_preview=False,
        )
        log.info(
            'cat: sticker %s on comment %s in %s',
            fallback,
            cat.reply_to,
            cat.chat,
        )

    async def _reply_in_thread(
        self, cat: cats.Cat, message: PremiumMessage
    ) -> None:
        """Send ``message`` as a reply inside the comment thread (top=root)."""
        reply = InputReplyToMessage(
            reply_to_msg_id=cat.reply_to, top_msg_id=cat.root
        )
        await self.client(
            SendMessageRequest(
                peer=cat.chat,
                message=message.text,
                entities=message.entities,
                reply_to=reply,
                no_webpage=True,
            )
        )

    async def _react(
        self, peer: int, msg_id: int, emojis: tuple[tuple[str, str], ...]
    ) -> bool:
        """Place the given premium cat(s) as a reaction ON ``msg_id``.

        ``emojis`` are the ``(id, fallback)`` cats chosen up front. The whole
        set goes in ONE ``SendReaction`` call (reactions are atomic: one
        request carries the account's whole reaction set on the message).
        Returns whether anything was placed (False for an empty set). Shared by
        the post-react and comment-react paths.
        """
        if not emojis:
            return False
        custom = [
            ReactionCustomEmoji(document_id=int(eid)) for eid, _ in emojis
        ]
        try:
            await self._send_reaction(peer, msg_id, custom)
        except Exception:  # noqa: BLE001 -- custom emoji may be disallowed
            # The chat may not allow CUSTOM-emoji reactions (or the account
            # is not Premium): fall back to the plain-emoji version of the
            # same cats (the fallback glyphs), so a cat reaction still lands
            # wherever standard reactions are allowed. If that fails too, it
            # propagates to the caller's guard (logged, never fatal).
            standard = [ReactionEmoji(emoticon=fb) for _, fb in emojis]
            await self._send_reaction(peer, msg_id, standard)
            log.info(
                'cat: custom reaction rejected in %s; used standard emoji',
                peer,
            )
        return True

    async def _send_reaction(
        self, peer: int, msg_id: int, reaction: list[object]
    ) -> None:
        """One SendReaction call placing ``reaction`` on ``msg_id``."""
        await self.client(
            SendReactionRequest(
                peer=peer,
                msg_id=msg_id,
                reaction=reaction,
                add_to_recent=True,
            )
        )

    async def help_report(self) -> None:
        """Send the plain-language command menu (/help and /start)."""
        await self.client.send_message(
            self.config.source, self.consts.help_text, link_preview=False
        )
        log.info('sent help menu to %s', self.config.source)

    async def enter_test(self) -> None:
        """Switch the whole bot to the TEST profile (the /test command)."""
        await self.switch_mode('test')

    async def enter_live(self) -> None:
        """Switch the whole bot to the LIVE profile (the /live command)."""
        await self.switch_mode('live')

    async def status_report(self) -> None:
        """Post the pending/posted/cat diagnostics to the source chat."""
        labels = await self._chat_labels()
        await self._send_status(self._status_text(labels))
        log.info('sent status report to %s', self.config.source)

    async def _send_status(self, text: str) -> None:
        """Send an operator report, rendering its premium-emoji markup.

        /status, /requeue and /catnow embed `<tg-emoji>` tags for the chosen
        cats/likes and the pool previews; build_premium_message turns them into
        custom-emoji entities, so the REAL premium emoji show (a non-premium
        viewer still sees the fallback glyph). Text without tags sends plain.
        """
        message = build_premium_message(text)
        await self.client.send_message(
            self.config.source,
            message.text,
            formatting_entities=message.entities,
            link_preview=False,
        )

    async def _chat_labels(self) -> dict[int, str]:
        """Resolve every chat shown in /status to a readable @name or title."""
        ids = {self.config.source, *self.config.targets}
        if self.config.test_target:
            ids.add(self.config.test_target)
        ids |= {chat for chat, _ in self.cats.posts}
        ids |= {v.peer_id for v in self._pending_views}  # story-view queue
        return {cid: await self._chat_label(cid) for cid in ids}

    async def _chat_label(self, chat_id: int) -> str:
        """Return a chat's @username (or "title") for /status, else id."""
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

    def _ic(self, key: str, fallback: str = '') -> str:
        """Return a /status glyph from the JSON, or the fallback."""
        return self.consts.status.get(key) or fallback

    def _dot(self, *, on: bool) -> str:
        """Return the green/red status dot."""
        return self._ic('on', '[on]') if on else self._ic('off', '[off]')

    def _bul(self) -> str:
        """Return the bullet glyph leading sub-lines and joining headers."""
        return self._ic('bullet', '-')

    def _arr(self) -> str:
        """Return the arrow glyph ('next ...' / 'posting ...')."""
        return self._ic('arrow', '->')

    def _head(self, key: str, label: str, *tail: str) -> str:
        """'icon label [ . tail . tail ]', skipping any blank piece."""
        title = ' '.join(p for p in (self._ic(key), label) if p)
        sep = f' {self._bul()} '
        return sep.join([title, *(t for t in tail if t)])

    def _status_text(self, labels: dict[int, str]) -> str:
        """Return status: header, routing, videos, cats, greeter, users."""
        flag = 'TEST' if self.mode == 'test' else 'LIVE'
        parts = [
            self._head('title', 'Aggregator', f'{self._dot(on=True)} {flag}'),
            '',
            *self._routing_lines(labels),
            '',
            *self._videos_lines(),
            '',
            *self._cat_status_lines(labels),
            '',
            *self._greeter_lines(),
            '',
            self._users_line(),
            *self._stories_lines(labels),
        ]
        if self.consts.status_help:
            legend = ' '.join(
                p for p in (self._ic('legend'), self.consts.status_help) if p
            )
            parts += ['', legend]
        return '\n'.join(parts)

    def _greeter_lines(self) -> list[str]:
        """Greeter section: on/off, DMs today, admin-log cursor, next check."""
        gp = self.greeter.params
        gs = self.greeter.state
        state = 'on' if gp.enabled else 'off'
        head = self._head(
            'greeter',
            'Greeter',
            f'{self._dot(on=gp.enabled)} {state}',
            f'DMs {gs.dm_today}/{gp.max_dm_per_day}',
            f'last event {gs.last_event_id}',
        )
        if not gp.enabled:
            return [head]
        return [head, self._greeter_schedule_line()]

    def _greeter_schedule_line(self) -> str:
        """Return the greeter's check period and next-check time (clock)."""
        period = int(self.greeter.params.poll_sec)
        nxt = self.greeter.next_sync
        b = self._bul()
        if nxt <= 0:
            return f'{b} check {period}s {b} next: first run'
        tz = timezone(timedelta(hours=self.cats.params.tz_offset_hours))
        clock = datetime.fromtimestamp(nxt, tz=tz).strftime('%H:%M')
        eta = nxt - time.time()
        when = 'now' if eta <= 0 else _fmt_eta(eta)
        return (
            f'{self._bul()} check {period}s {self._arr()} '
            f'next {clock} (in {when})'
        )

    def _routing_lines(self, labels: dict[int, str]) -> list[str]:
        """Source, the live targets, and where posts go NOW (test vs live)."""
        source = labels.get(self.config.source, str(self.config.source))
        targets = ', '.join(labels.get(t, str(t)) for t in self.config.targets)
        dest = ', '.join(labels.get(t, str(t)) for t in self.live_targets())
        b = self._bul()
        return [
            self._head('routing', 'Routing'),
            f'{b} source: {source}',
            f'{b} target: {targets}',
            f'{b} posting {self._arr()} {dest}',
        ]

    def _guard_desc(self) -> str:
        """One-line summary of the active re-post guard windows.

        Shows both windows so the operator can confirm dedup is armed: the
        time window (e.g. '7d') and the count window (e.g. 'last 5'). Each
        reads 'off' when its knob is 0; 'off' overall when both are.
        """
        secs = self.config.repost_guard
        count = self.config.repost_guard_count
        time_part = _fmt_eta(secs) if secs > 0 else 'off'
        count_part = f'last {count}' if count > 0 else 'off'
        if secs <= 0 and count <= 0:
            return 'off'
        return f'{time_part}/{count_part}'

    def _videos_lines(self) -> list[str]:
        """Videos: counts on the header, then pending + recent posts."""
        b = self._bul()
        window = _fmt_eta(self.config.timeout)
        lines = [
            self._head(
                'videos',
                'Videos',
                f'pending {len(self.groups)} (timeout {window})',
                f'posted {len(self.posted)}',
                f'rejected {len(self.rejected)}',
                f'guard {self._guard_desc()}',
            )
        ]
        for group in self.groups:
            have = ', '.join(sorted(group.items)) or '-'
            missing = (
                ', '.join(
                    p for p in self.config.platforms if p not in group.items
                )
                or 'complete'
            )
            left = self.config.timeout - (time.time() - group.created_at)
            lines.append(
                f'{b} "{_trim(group.title)}" have [{have}] wait [{missing}]'
                f' {self._arr()} ~{_fmt_eta(left)}'
            )
        lines.extend(
            f'{b} "{_trim(post.title)}" {b} {post.at[:10]}'
            f' {b} {len(post.links)} links'
            for post in self.posted[-5:]
        )
        return lines

    def _cat_status_lines(self, labels: dict[int, str]) -> list[str]:
        """Return the cat engine's live state (empty-ish when disabled)."""
        brain = self.cats
        b = self._bul()
        enabled = brain.params.enabled
        state = 'on' if enabled else 'off'
        window = f'{brain.params.active_start:g}-{brain.params.active_end:g}h'
        alive = brain.state.alive
        top = sorted(alive, key=lambda h: alive[h], reverse=True)[:6]
        learned = ', '.join(f'{h}h' for h in top) or '(learning)'
        likes = _pool_markup(brain.params.like_pool)
        pool = _pool_markup(brain.params.pool)
        return [
            self._head(
                'cats',
                'Cats',
                f'{self._dot(on=enabled)} {state}',
                f'{len(brain.params.pool)} cats / '
                f'{len(brain.params.like_pool)} likes',
            ),
            f'{b} likes {self._arr()} {likes}',
            f'{b} cats {self._arr()} {pool}',
            (
                f'{b} mood {brain.state.mood:.2f} {b} answered '
                f'{len(brain.state.catted)} {b} pending '
                f'{len(brain.state.pending)}'
            ),
            f'{b} window {window} (prior) {b} learned {learned}',
            self._cat_rescan_line(),
            *self._last_posts_lines(labels),
            *self._pending_cat_lines(),
            f'{b} /catnow {b} /requeue',
        ]

    def _cat_rescan_line(self) -> str:
        """Return the auto-rescan period and the countdown to the next one."""
        b = self._bul()
        period = int(self._rescan_sec)
        if period <= 0:
            return f'{b} rescan: off (use /requeue)'
        nxt = self._cat_next_rescan
        if nxt <= 0:
            return f'{b} rescan {period}s {b} next: first run'
        tz = timezone(timedelta(hours=self.cats.params.tz_offset_hours))
        clock = datetime.fromtimestamp(nxt, tz=tz).strftime('%H:%M')
        eta = nxt - time.time()
        when = 'now' if eta <= 0 else _fmt_eta(eta)
        return f'{b} rescan {period}s {self._arr()} next {clock} (in {when})'

    def _pending_cat_lines(self) -> list[str]:
        """Return the queued cats: which cat lands on which comment, when."""
        pending = self.cats.state.pending
        if not pending:
            return []
        now = time.time()
        lines = [f'{self._bul()} queued:']
        lines.extend(
            self._pending_cat_line(entry, now)
            for entry in pending[:STATUS_PENDING_CATS]
        )
        extra = len(pending) - STATUS_PENDING_CATS
        if extra > 0:
            lines.append(f'    ... (+{extra} more)')
        return lines

    def _pending_cat_line(self, entry: dict[str, object], now: float) -> str:
        """One queued line: '<cat> <verb> -> <comment> . post N . <eta>'."""
        b = self._bul()
        msg = int(entry.get('reply_to', 0))
        root = int(entry.get('root', msg))
        body = str(entry.get('text', ''))
        what = f'"{body}"' if body else f'comment {msg}'
        glyphs = _pending_markup(entry)
        verb = 'sticker' if entry.get('kind') == 'reply' else 'like'
        eta = float(entry.get('when', now)) - now
        when = 'due now' if eta <= 0 else f'in ~{_fmt_eta(eta)}'
        return (
            f'    {glyphs} {verb} {self._arr()} {what}'
            f' {b} post {root} {b} {when}'
        )

    def _last_posts_lines(self, labels: dict[int, str]) -> list[str]:
        """Return the watched comment threads, grouped one line per chat."""
        posts = self.cats.posts
        if not posts:
            return []
        by_chat: dict[int, list[int]] = {}
        for chat, mid in posts:
            by_chat.setdefault(chat, []).append(mid)
        lines = [f'{self._bul()} watching {len(posts)} posts:']
        lines.extend(
            f'    {labels.get(chat, str(chat))}: '
            f'{", ".join(str(m) for m in mids)}'
            for chat, mids in by_chat.items()
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
        """Periodically log pending videos, learn uptime, beat the watchdog."""
        while True:
            await asyncio.sleep(STATUS_INTERVAL)
            await self._heartbeat()
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

    async def _heartbeat(self) -> None:
        """Prove end-to-end liveness, then refresh the watchdog heartbeat.

        A cheap Telegram round-trip (``get_me``) under a timeout: on success we
        are both loop-alive AND actually talking to Telegram, so we stamp the
        health file. On a hang/timeout we skip the stamp, letting the file age
        until the watchdog thread restarts the process. Reaching this line at
        all already proves the event loop is not stalled.
        """
        try:
            await asyncio.wait_for(
                self.client.get_me(), timeout=self._probe_timeout
            )
        except Exception:  # noqa: BLE001 -- wedged/unreachable: let it go stale
            log.warning('watchdog: liveness probe failed; heartbeat stale')
            return
        _touch_health()

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

    def live_targets(self) -> tuple[int, ...]:
        """Post destination for the active profile.

        Test: TEST_CHAT_ID, or the source control chat if it is unset. Live:
        the configured targets. Every channel-touching part reads this, so the
        whole bot follows the profile.
        """
        if self.mode == 'test':
            return (self.config.test_target or self.config.source,)
        return self.config.targets

    async def _deliver(
        self, message: PremiumMessage, thumb: str
    ) -> list[tuple[int, int]]:
        """Send the post to every target; return (target, post_id) delivered.

        Only the SEND is here -- the caller records state from the returned
        list, then does the react/watch steps. A send that raises (flood wait,
        network) is logged and skipped so one bad target neither aborts the
        others nor blocks recording the ones that did go out. An empty result
        means nothing was delivered and the caller should re-queue.
        """
        posts: list[tuple[int, int]] = []
        for target in self.live_targets():
            try:
                sent = await self._send_post(target, message, thumb)
            except Exception:
                log.exception('send to %s failed', target)
                continue
            posts.append((target, int(getattr(sent, 'id', 0) or 0)))
        return posts

    async def _react_to_post(self, target: int, post_id: int) -> None:
        """Immediately place a cat reaction ON a freshly-posted message.

        Optional (``react_to_posts``, default off): reacting to our own posts
        is an extra, separate from the engine's real job of reacting to
        commenters. When on, there is no human-like wait -- the post is ours,
        so the cat goes on straight away. A failure never blocks the post -- it
        is logged and swallowed, unlike the comment path which owns its own
        retry/skip machinery.
        """
        if not self.cats.params.enabled or not post_id:
            return
        if not self.cats.params.react_to_posts:
            return
        specs = self.cats.pick_like(f'{target}:{post_id}')
        emojis = tuple((s.emoji_id, s.fallback) for s in specs)
        try:
            placed = await self._react(target, post_id, emojis)
        except Exception:  # noqa: BLE001 -- reacting must never break posting
            log.warning(
                'cat: could not react to new post %s in %s', post_id, target
            )
            return
        if placed:
            glyphs = ''.join(fb for _, fb in emojis)
            log.info(
                'cat: reacted %s to new post %s in %s', glyphs, post_id, target
            )

    async def _watch_post(self, target: int, post_id: int) -> None:
        """Register where comments on this post will appear (the cat target).

        For a channel with a linked discussion (comments_in_discussion), the
        comments live in the discussion group: resolve the post's thread root
        and watch THAT, so cats land only in the channel post's comments. For a
        plain group target, the post message id itself is the comment target.
        """
        if self.cats.params.comments_in_discussion:
            # Channel: only watch a post whose discussion thread resolves; a
            # post with comments off (or deleted) adds nothing rather than a
            # useless channel-id entry that could evict a real one.
            thread = await self._discussion_thread(target, post_id)
            if thread is not None:
                self.cats.note_post(*thread)
            return
        self.cats.note_post(target, post_id)

    async def _throttle_discussion(self) -> None:
        """Space out GetDiscussionMessageRequest calls (Telegram flood guard).

        These resolve a post's comment thread and are fired in bursts -- the
        last ``watch_posts`` posts per target on every startup and rescan --
        which trips Telegram's flood wait. Keep at least ``discussion_gap``
        seconds between consecutive calls, process-wide. 0 disables it.
        """
        gap = self.config.discussion_gap
        if gap <= 0:
            return
        wait = gap - (time.time() - self._last_discussion_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_discussion_ts = time.time()

    async def _discussion_thread(
        self, channel: int, post_id: int
    ) -> tuple[int, int] | None:
        """(discussion_chat_id, thread_root_id) for a channel post, or None."""
        await self._throttle_discussion()
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
            'restored %d pending, %d posted (%d dedup ids); mode=%s',
            len(self.groups),
            len(self.posted),
            len(self.processed_ids),
            self.mode,
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
    """Return the monitoring (source) chat id from env, else default."""
    return int(os.environ.get('SOURCE_CHAT_ID') or DEFAULT_SOURCE_CHAT_ID)


def _targets() -> tuple[int, ...]:
    """Return the target chat ids from env (comma-separated), else default.

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


def _test_target() -> int:
    """Return the test channel id from TEST_CHAT_ID (0 = test off)."""
    return int(os.environ.get('TEST_CHAT_ID') or 0)


def _load_config() -> Config:
    """Chats come from the env; all behaviour from the constants JSON."""
    data = _read_json(Path(__file__).with_name(CONSTANTS_FILE))
    csv = str(data.get('platforms') or DEFAULT_PLATFORMS)
    platforms = tuple(p.strip().lower() for p in csv.split(',') if p.strip())
    return Config(
        source=_source(),
        targets=_targets(),
        test_target=_test_target(),
        platforms=platforms,
        threshold=float(data.get('title_match') or 0.9),
        # Three hours by default: platforms can arrive far apart. The wait is
        # a local timer (asyncio.sleep), so it costs Telegram nothing.
        timeout=float(data.get('timeout_sec') or 10800),
        # Recent source messages to scan at startup for unprocessed ones.
        backfill=int(data.get('backfill') or 100),
        # A video whose known duration reaches this many seconds is dropped.
        max_duration=int(data.get('max_duration_sec') or MAX_SHORT_SEC),
        # A week by default: a title posted in the last week is not posted
        # again (matches a typical chat auto-delete window). 0 disables.
        repost_guard=float(data.get('repost_guard_sec', 604800)),
        # Also block a title matching any of the last N posted videos, no
        # matter how long ago -- a clock-independent floor so a re-delivered
        # video cannot slip back in until N distinct videos have gone out.
        # This is what survives the worst case: the first copy posts on the
        # timeout, then the same title's later platforms (or an auto-delete
        # re-emit) are all skipped instead of forming a fresh post. 5 by
        # default; 0 disables this window and leaves only the time guard.
        repost_guard_count=int(data.get('repost_guard_count', 5)),
        # Space out discussion-thread lookups so cat seeding on startup/rescan
        # does not trip Telegram flood waits. 2s by default; 0 disables.
        discussion_gap=float(data.get('discussion_gap_sec', 2.0)),
    )


async def main() -> None:
    """Listen to the source chat and aggregate videos across platforms."""
    load_env()

    api_id = os.environ.get('TELEGRAM_API_ID')
    api_hash = os.environ.get('TELEGRAM_API_HASH')
    if not api_id or not api_hash:
        msg = 'Set TELEGRAM_API_ID and TELEGRAM_API_HASH.'
        raise SystemExit(msg)
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
    # Hydrate the active profile (live or test) and start its loops. The cat
    # startup inside can fail without stopping the bot from listening.
    await agg.start_profile()
    log.info(
        'Listening on %s; mode=%s posting to %s; platforms=%s',
        config.source,
        agg.mode,
        ','.join(str(t) for t in agg.live_targets()),
        ','.join(config.platforms),
    )
    status_task = asyncio.create_task(agg.status_loop())
    # Self-healing watchdog: seed the heartbeat now (so a cold start is not
    # instantly "stale"), then a daemon thread exits the process if the
    # heartbeat later goes stale -- a hang no restart: policy could catch --
    # so Docker's restart: always recreates the container.
    _touch_health()
    watchdog_sec = float(_load_runtime().get('watchdog_sec', 600.0))
    threading.Thread(
        target=_watchdog, args=(watchdog_sec,), daemon=True
    ).start()
    await client.run_until_disconnected()
    status_task.cancel()
    await agg.stop_profile()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info('Stopped.')
