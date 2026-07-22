"""Runtime admin config: operator-tunable knobs in one STATE JSON.

The moderator bot writes these from chat; every bot reads them at its
natural point -- a streaming bot each loop, a batch bot at the start of a
run -- so schedules and toggles change with no redeploy. Only non-secret
operator knobs live here; tokens, chat ids and URLs stay in the env.

One registry (``SETTINGS``) is the whole surface: a key, its default and
a one-line help. The moderator lists it verbatim, so the admin panel and
the code can never drift.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from minion_core.kernel import atomic_write

if TYPE_CHECKING:
    from pathlib import Path

ADMIN_FILE = 'admin.json'
"""Where the runtime config lives under a STATE directory."""


@dataclass(frozen=True)
class Setting:
    """One tunable knob: its key, default value and a one-line help."""

    key: str
    default: str
    help: str


SETTINGS: tuple[Setting, ...] = (
    Setting(
        'donation_platform',
        'streamlabs',
        'donations: platforms, comma list (streamlabs,revolut)',
    ),
    Setting(
        'donation_chat',
        '',
        'donations: chat the alerts post into (blank = env DONATION_CHAT)',
    ),
    Setting(
        'donation_poll_sec',
        '10',
        'donations: seconds between feed polls (restart to apply)',
    ),
    Setting(
        'bed_broadcast_sec',
        '0',
        'donations: bed auto-post interval in seconds (0 = off)',
    ),
    Setting(
        'bed_chat',
        '',
        'donations: chat for the bed auto-post (blank = DONATION_CHAT)',
    ),
    Setting(
        'wishlist_url',
        '',
        'wishlist: the public wishlist URL (blank = env WISHLIST_URL)',
    ),
    Setting(
        'wishlist_chat',
        '',
        'wishlist: chat gifts/adds post into (blank = env WISHLIST_CHAT)',
    ),
    Setting('wishlist_enabled', '1', 'wishlist: run the daily scan (1/0)'),
    Setting(
        'wishlist_announce',
        '0',
        'wishlist: post the full list every run (1/0)',
    ),
    Setting(
        'week_clean_enabled',
        '1',
        'week-clean: run the Monday shelving (1/0)',
    ),
)

_DEFAULTS: dict[str, str] = {s.key: s.default for s in SETTINGS}

KEYS: frozenset[str] = frozenset(_DEFAULTS)
"""Every settable key -- the moderator validates against this."""


@dataclass(frozen=True)
class AdminConfig:
    """The runtime config file: defaults overlaid with stored overrides."""

    path: Path

    def _stored(self) -> dict[str, str]:
        """The overrides on disk, unknown keys dropped."""
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            key: str(value)
            for key, value in data.items()
            if isinstance(key, str) and key in _DEFAULTS
        }

    def all(self) -> dict[str, str]:
        """Every setting's effective value (default, then any override)."""
        return {**_DEFAULTS, **self._stored()}

    def get(self, key: str) -> str:
        """One setting's effective value, or '' for an unknown key."""
        return self.all().get(key, '')

    def effective(self, key: str, fallback: str) -> str:
        """An explicit override if set, else ``fallback`` (e.g. an env var).

        Lets a non-secret env value seed a setting for a smooth migration:
        the moderator's override wins, otherwise the env/default stands.
        """
        return self._stored().get(key, fallback)

    def set(self, key: str, value: str) -> bool:
        """Store an override; False (and no write) for an unknown key."""
        if key not in _DEFAULTS:
            return False
        stored = self._stored()
        stored[key] = value
        atomic_write(
            self.path, json.dumps(stored, ensure_ascii=False).encode('utf-8')
        )
        return True

    def reset(self, key: str) -> bool:
        """Drop an override back to its default; False for an unknown key."""
        if key not in _DEFAULTS:
            return False
        stored = self._stored()
        stored.pop(key, None)
        atomic_write(
            self.path, json.dumps(stored, ensure_ascii=False).encode('utf-8')
        )
        return True


def admin_config(state: Path) -> AdminConfig:
    """The runtime config stored under a STATE directory."""
    return AdminConfig(state / ADMIN_FILE)
