"""One frozen Settings, built once in main() (BLUEPRINT 7).

No module-level environment reads; precedence is the mapping you
pass. ``load`` coerces one line per field and raises on any relative
path override (REQ-CFG-001) -- a raise, not an assert, so it survives
``python -O``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

CHAT = 'chat'
"""fetch sink axis: send the downloaded file back to the chat."""

QUEUE = 'queue'
"""fetch sink axis: park the downloaded file in the fan queue."""

MODE_BLUR = 'blur'
MODE_BLACK = 'black'
MODE_RESTORE = 'restore'
"""censor mode axis values (BLUEPRINT 9)."""

UNKNOWN = 'Unknown'
"""The fandom folder sparse fandoms demote into (BLUEPRINT 9)."""


class BadConfig(ValueError):
    """Invalid override; the loud start-refusal of REQ-CFG-001."""


_DEFAULTS: dict[str, str] = {
    'DOWNLOAD_TIMEOUT_SEC': '900',
    'QUOTA_BYTES': str(32 * 1024**3),
    'MAX_EMBEDDING_SCAN': '5000',
    'SEEN_PATHS_MAX': '4096',
    'FRAME_STRIDE': '30',
    'DEMOTE_MIN_COUNT': '3',
    'YTDLP_FORMAT': 'bestvideo*+bestaudio/best',
    'YTDLP_CONTAINER': 'mkv',
    'YTDLP_PLAYER_CLIENTS': 'default',
    'SOURCE_DIRS': '',
    'CENSOR_MODE': MODE_BLUR,
    'FETCH_SINK': CHAT,
    'WEEK_TAG': 'bananaland:week',
    'POLL_SEC': '2.0',
}


@dataclass(frozen=True)
class Settings:
    """Every tunable of the system; passed down as one value."""

    drive: Path
    download_timeout_sec: int
    quota_bytes: int
    max_embedding_scan: int
    seen_paths_max: int
    frame_stride: int
    demote_min_count: int
    ytdlp_format: str
    ytdlp_container: str
    ytdlp_player_clients: tuple[str, ...]
    source_dirs: tuple[Path, ...]
    censor_mode: str
    fetch_sink: str
    week_tag: str
    poll_sec: float

    # Derived, never overridable separately (BLUEPRINT 1.2).
    @property
    def inbox(self) -> Path:
        """Ingest drop folder [MEDIA]."""
        return self.drive / '_inbox'

    @property
    def pictures(self) -> Path:
        """Sorted library root [MEDIA]."""
        return self.drive / 'pictures'

    @property
    def state(self) -> Path:
        """Durable per-bot state [STATE]; never deleted."""
        return self.drive / 'bots' / '_data' / 'state'

    @property
    def regen(self) -> Path:
        """Model weights + embeddings [CACHE]; disposable."""
        return self.drive / 'bots' / '_data' / 'regen'

    @property
    def logs(self) -> Path:
        """Append-only evidence [TELEMETRY]."""
        return self.drive / 'bots' / '_data' / 'logs'

    @property
    def print_queue(self) -> Path:
        """Print queue [MEDIA]."""
        return self.drive / 'print'

    @property
    def print_done(self) -> Path:
        """Print archive [MEDIA]."""
        return self.drive / 'print' / '_done'

    @property
    def scripts(self) -> Path:
        """Weekly document archive [MEDIA]."""
        return self.drive / 'Scripts'

    def bot_dir(self, name: str) -> Path:
        """A bot's work directory [MEDIA]."""
        return self.drive / 'bots' / name

    def bot_done(self, name: str) -> Path:
        """A bot's archive directory [MEDIA]."""
        return self.bot_dir(name) / 'done'


def load(env: Mapping[str, str]) -> Settings:
    """Build Settings from a mapping; coerce one line per field."""
    def get(key: str) -> str:
        return env.get(key, _DEFAULTS.get(key, ''))

    drive = get('DRIVE')
    if not drive:
        raise BadConfig('bad_config: DRIVE is required')
    return Settings(
        drive=_abs('DRIVE', Path(drive)),
        download_timeout_sec=int(get('DOWNLOAD_TIMEOUT_SEC')),
        quota_bytes=int(get('QUOTA_BYTES')),
        max_embedding_scan=int(get('MAX_EMBEDDING_SCAN')),
        seen_paths_max=int(get('SEEN_PATHS_MAX')),
        frame_stride=int(get('FRAME_STRIDE')),
        demote_min_count=int(get('DEMOTE_MIN_COUNT')),
        ytdlp_format=get('YTDLP_FORMAT'),
        ytdlp_container=get('YTDLP_CONTAINER'),
        ytdlp_player_clients=_csv(get('YTDLP_PLAYER_CLIENTS')),
        source_dirs=_dirs(get('SOURCE_DIRS')),
        censor_mode=get('CENSOR_MODE'),
        fetch_sink=get('FETCH_SINK'),
        week_tag=get('WEEK_TAG'),
        poll_sec=float(get('POLL_SEC')),
    )


def _abs(name: str, path: Path) -> Path:
    """Reject any relative path override at load (REQ-CFG-001)."""
    if not path.is_absolute():
        raise BadConfig(f'bad_config: {name} must be absolute: {path}')
    return path


def _csv(raw: str) -> tuple[str, ...]:
    """Split a comma-separated value list."""
    return tuple(part.strip() for part in raw.split(',') if part.strip())


def _dirs(raw: str) -> tuple[Path, ...]:
    """Split a ';'-separated path list; every entry must be absolute."""
    parts = (part.strip() for part in raw.split(';'))
    return tuple(_abs('SOURCE_DIRS', Path(p)) for p in parts if p)
