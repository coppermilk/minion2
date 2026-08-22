# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Bootstrap: env, the constants JSON, path resolution, and the Config.

Extracted from ``main`` so the deploy-time wiring (chats from the env,
behaviour from ``aggregator_constants.json``, where the session/state live)
sits apart from the runtime aggregator. Depends only on the models and the
stdlib -- no Telethon -- so it imports cleanly in tests and helper scripts.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from minions.aggregator.models import DEFAULT_FIELDS
from minions.aggregator.models import Config
from minions.aggregator.models import Consts

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
# The persisted runtime feature overrides (a name -> bool map) live here, in
# the base state dir so both profiles share one choice (like the JSON enabled).
FEATURE_OVERRIDES_FILE = 'feature_overrides.json'
# The project keeps ONE .env at the repo root (compose's env_file and the
# Windows launcher both point there). This package is minions/aggregator/, so
# parents[2] is that repo root. In Docker the vars are already in os.environ
# (compose env_file), so a missing file here is harmless; env always wins.
PROJECT_ENV = Path(__file__).resolve().parents[2] / '.env'
# Last-resort file-session base path: 'telethon.session' next to this package.
# It is git-ignored, so a session file kept in the repo checkout survives a
# repo re-sync (deploy/nas-update.sh's `git reset --hard`), exactly like .env.
# Telethon appends '.session', so the file on disk is 'telethon.session'.
DEFAULT_SESSION_PATH = Path(__file__).with_name('telethon')


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


def _str_list(value: object, default: str) -> list[str]:
    """Return a list of label strings from a JSON list (or a single string)."""
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value] if value else [default]


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


def _load_runtime() -> dict[str, object]:
    """Return the 'runtime' section of the constants JSON, or {}."""
    data = _read_json(Path(__file__).with_name(CONSTANTS_FILE))
    rt = data.get('runtime')
    return rt if isinstance(rt, dict) else {}


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
