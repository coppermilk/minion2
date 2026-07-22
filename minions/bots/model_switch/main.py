"""moderator bot: control the system from chat.

A control bot, not a belt. It answers text commands:

- ``local`` / ``gemini`` / ``status`` flip (or report) the STATE toggle
  that sort and props read, so the whole system swaps model with no
  restart (restore is unaffected -- Gemini-only image generation);
- ``clean`` runs the week-clean shelving on demand -- the same routine
  the Monday cron runs -- so the operator need not wait for Monday.
"""

from __future__ import annotations

import functools
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from minion_core.adapters.backend import CHOICES
from minion_core.adapters.backend import GEMINI
from minion_core.adapters.backend import LOCAL
from minion_core.adapters.backend import BackendToggle
from minion_core.adapters.donations import bed_roster
from minion_core.adapters.files import free_quota
from minion_core.adapters.library import clean_week
from minion_core.adapters.tg import SpoolSpec
from minion_core.adapters.tg import TgApi
from minion_core.adapters.tg import TgCommands
from minion_core.adapters.tg import TgSpec
from minion_core.adapters.tg import chats_from
from minion_core.adapters.wishlist import SnapshotStore
from minion_core.kernel import bot_logger
from minion_core.kernel import run
from minion_core.settings import load

if TYPE_CHECKING:
    import logging
    from collections.abc import Mapping

    from minion_core.kernel import Stage
    from minion_core.settings import Settings

BOT = 'model-switch'

_STATUS = ('status', 'which', '')
_CLEAN = ('clean', 'clean now', 'cleanup')
_MENU_WORDS = ('menu', 'help', 'panel', 'admin', 'commands')
_BED_WORDS = ('bed', 'under the bed')
_WISHLIST_WORDS = ('wishlist', 'wl')

_MENU = (
    'moderator panel\n'
    '\n'
    'backend (classify/props model):\n'
    '  local | gemini  - switch the model\n'
    '  status          - which backend is live\n'
    '  clean           - run week-clean now\n'
    '\n'
    'donations bot:\n'
    '  bed             - who is under the bed (last 7 days)\n'
    '\n'
    'wishlist bot:\n'
    '  wishlist        - how many items are tracked\n'
    '\n'
    'menu | help       - show this panel'
)


def reply_for(toggle: BackendToggle, text: str) -> str:
    """Map a backend command to a reply, flipping the toggle on a word."""
    word = text.strip().lower()
    if word in CHOICES:
        toggle.write(word)
        return f'backend set to {word}'
    if word in _STATUS:
        return f'backend is {toggle.read()}'
    return f'send one of: {LOCAL}, {GEMINI}, status, clean, menu'


@dataclass(frozen=True)
class _Moderator:
    """The admin panel: backend, on-demand clean, and bot status reads."""

    cfg: Settings
    toggle: BackendToggle
    log: logging.Logger

    def __call__(self, text: str) -> str:
        """Route one command word to its panel action."""
        word = text.strip().lower()
        if word in _MENU_WORDS:
            return _MENU
        if word in _BED_WORDS:
            return self._bed()
        if word in _WISHLIST_WORDS:
            return self._wishlist()
        if word in _CLEAN:
            return self._clean()
        return reply_for(self.toggle, text)

    def _clean(self) -> str:
        """Run the week-clean shelving on demand (the Monday routine)."""
        if clean_week(self.cfg, self.log):
            return 'cleaning now -- shelving the classified week'
        return 'a clean is already running; try again shortly'

    def _bed(self) -> str:
        """Who is under the donations bed right now (last seven days)."""
        names = bed_roster(self.cfg.state).active(time.time())
        if not names:
            return 'the bed is empty -- no donors in the last 7 days'
        roll = ', '.join(name or 'anon' for name in names)
        return f'under the bed ({len(names)}): {roll}'

    def _wishlist(self) -> str:
        """How many items the wishlist bot is currently tracking."""
        items = SnapshotStore(self.cfg.state / 'wishlist.json').load()
        return f'wishlist: {len(items)} items tracked'


def build(cfg: Settings, env: Mapping[str, str]) -> Stage:
    """Assemble the command dock; the handler is bound to cfg + toggle."""
    api = TgApi(env.get('TG_TOKEN', ''))
    spec = TgSpec(
        spool=SpoolSpec(
            into=cfg.bot_dir(BOT), budget=functools.partial(free_quota, cfg)
        ),
        dest=cfg.bot_dir(BOT),
        offset=cfg.state / f'{BOT}.offset',
        chats=chats_from(env),
    )
    handle = _Moderator(cfg, BackendToggle(cfg), bot_logger(BOT, cfg.logs))
    return TgCommands(api, spec, handle)


def main(env: Mapping[str, str] | None = None) -> int:
    """Build Settings once, service commands forever (tokenless: idle)."""
    mapping = os.environ if env is None else env
    cfg = load(mapping)
    return run(BOT, build(cfg, mapping), cfg.logs)


if __name__ == '__main__':
    raise SystemExit(main())
