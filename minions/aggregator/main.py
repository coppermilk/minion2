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
import json
import logging
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from telethon import TelegramClient
from telethon import events
from telethon.tl.functions.messages import GetDiscussionMessageRequest

from minions.aggregator import cats
from minions.aggregator import comod
from minions.aggregator import greeter
from minions.aggregator import stories
from minions.aggregator import users
from minions.aggregator.cats_glue import _CatsMixin
from minions.aggregator.comod_glue import _ComodMixin
from minions.aggregator.matching import _action_ok
from minions.aggregator.matching import _duration_seconds
from minions.aggregator.matching import _extract_fields
from minions.aggregator.matching import _is_recent_repost
from minions.aggregator.matching import _norm
from minions.aggregator.matching import _parse_item
from minions.aggregator.matching import _similar
from minions.aggregator.matching import _thread_top
from minions.aggregator.models import _THUMB_ALIASES
from minions.aggregator.models import DEFAULT_FIELDS
from minions.aggregator.models import Config
from minions.aggregator.models import Consts
from minions.aggregator.models import Group
from minions.aggregator.models import Item
from minions.aggregator.models import Posted
from minions.aggregator.models import _iso
from minions.aggregator.premium_emoji import build_premium_message
from minions.aggregator.render import _compose
from minions.aggregator.render import _render_constants
from minions.aggregator.render import _sample_groups
from minions.aggregator.render import _youtube_thumb
from minions.aggregator.runtime import _cancel
from minions.aggregator.runtime import _touch_health
from minions.aggregator.runtime import _watchdog
from minions.aggregator.runtime import configure_logging
from minions.aggregator.statefile import _pending_dict
from minions.aggregator.statefile import _pending_from_dict
from minions.aggregator.statefile import _posted_dict
from minions.aggregator.statefile import _posted_from_dict
from minions.aggregator.status import _StatusMixin
from minions.aggregator.status import _user_label
from minions.aggregator.stories_glue import _StoriesMixin

if TYPE_CHECKING:

    from minions.aggregator.premium_emoji import PremiumMessage


def _load_runtime() -> dict[str, object]:
    """Return the 'runtime' section of the constants JSON, or {}."""
    data = _read_json(Path(__file__).with_name(CONSTANTS_FILE))
    rt = data.get('runtime')
    return rt if isinstance(rt, dict) else {}


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


class Aggregator(_StatusMixin, _ComodMixin, _StoriesMixin, _CatsMixin):
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
        posts = await self._deliver_post(message, _youtube_thumb(group))
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


    async def greet_now(self) -> None:
        """Force the greeter to poll+process now (the /greetnow command)."""
        summary = await self.greeter.sync_now()
        await self.client.send_message(self.config.source, summary)
        log.info('greetnow: %s', summary)


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

    async def _deliver_post(
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
    """Chats from the env; behaviour from the constants JSON, validated.

    A bad constants file (a non-numeric knob, an out-of-range threshold, no
    platforms) fails fast here with a message naming the problem, instead of
    a confusing crash later or a bot that silently never completes a group.
    """
    data = _read_json(Path(__file__).with_name(CONSTANTS_FILE))
    csv = str(data.get('platforms') or DEFAULT_PLATFORMS)
    platforms = tuple(p.strip().lower() for p in csv.split(',') if p.strip())
    try:
        config = Config(
            source=_source(),
            targets=_targets(),
            test_target=_test_target(),
            platforms=platforms,
            threshold=float(data.get('title_match') or 0.9),
            # Three hours by default: platforms can arrive far apart. The wait
            # is a local timer (asyncio.sleep), so it costs Telegram nothing.
            timeout=float(data.get('timeout_sec') or 10800),
            # Recent source messages to scan at startup for unprocessed ones.
            backfill=int(data.get('backfill') or 100),
            # A video at/above this many seconds is dropped (not a Short).
            max_duration=int(data.get('max_duration_sec') or MAX_SHORT_SEC),
            # A week by default: a title posted in the last week is not posted
            # again (matches a typical chat auto-delete window). 0 disables.
            repost_guard=float(data.get('repost_guard_sec', 604800)),
            # Also block a title matching any of the last N posted videos, no
            # matter how long ago -- a clock-independent floor so a
            # re-delivered video cannot slip back in until N distinct videos
            # have gone out. Survives the worst case: the first copy posts on
            # the timeout, then the same title's later platforms (or an
            # auto-delete re-emit) are skipped, not re-posted. 5 by default;
            # 0 disables this window and leaves only the time guard.
            repost_guard_count=int(data.get('repost_guard_count', 5)),
            # Space out discussion-thread lookups so cat seeding on
            # startup/rescan does not trip flood waits. 2s default; 0 disables.
            discussion_gap=float(data.get('discussion_gap_sec', 2.0)),
        )
    except (TypeError, ValueError) as exc:
        msg = f'{CONSTANTS_FILE}: a numeric knob is not a number ({exc})'
        raise SystemExit(msg) from exc
    _validate_config(config)
    return config


def _validate_config(config: Config) -> None:
    """Fail fast on a nonsensical constants file (range/emptiness checks)."""
    problems: list[str] = []
    if not config.platforms:
        problems.append('platforms is empty')
    if not 0.0 < config.threshold <= 1.0:
        problems.append(
            f'title_match must be in (0, 1], got {config.threshold}'
        )
    non_negative = (
        ('timeout_sec', config.timeout),
        ('max_duration_sec', float(config.max_duration)),
        ('backfill', float(config.backfill)),
        ('repost_guard_sec', config.repost_guard),
        ('repost_guard_count', float(config.repost_guard_count)),
        ('discussion_gap_sec', config.discussion_gap),
    )
    problems += [
        f'{name} must be >= 0, got {value}'
        for name, value in non_negative
        if value < 0
    ]
    if problems:
        msg = f'{CONSTANTS_FILE}: ' + '; '.join(problems)
        raise SystemExit(msg)


async def main() -> None:
    """Listen to the source chat and aggregate videos across platforms."""
    configure_logging()
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
