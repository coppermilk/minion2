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
import html
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from minion_core.adapters.admin import KEYS
from minion_core.adapters.admin import SETTINGS
from minion_core.adapters.admin import AdminConfig
from minion_core.adapters.admin import admin_config
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
from minion_core.adapters.tg import chat_title
from minion_core.adapters.tg import chats_from
from minion_core.adapters.wishlist import SnapshotStore
from minion_core.kernel import bot_logger
from minion_core.kernel import run
from minion_core.settings import load

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable
    from collections.abc import Mapping

    from minion_core.kernel import Stage
    from minion_core.settings import Settings

BOT = 'moderator'

_STATUS = ('status', 'which', '')
_CLEAN = ('clean', 'clean now', 'cleanup')
_VERBS = ('set', 'get', 'reset', 'whois')
_VERB_PARTS = 2

_MENU = (
    '<b>moderator panel</b>\n'
    '\n'
    'backend (classify/props):\n'
    '  local | gemini | status\n'
    '  clean               - run week-clean now\n'
    '\n'
    'settings (persisted to admin.json):\n'
    '  config              - table of every setting and its value\n'
    '  set [key] [value]   - change a setting\n'
    '  reset [key]         - back to default\n'
    '\n'
    'status / lookups:\n'
    '  bed                 - who is under the bed (last 7 days)\n'
    '  wishlist            - how many items are tracked\n'
    '  whois [chat_id]     - resolve a chat id to a name\n'
    '\n'
    'menu | help           - show this panel'
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


def _unknown(key: str) -> str:
    """The reply for a setting key the registry does not know."""
    return f'unknown setting "{html.escape(key)}"; send config for the list'


def _set_reply(admin: AdminConfig, key: str, value: str) -> str:
    """Store a setting and confirm, or name it unknown."""
    if admin.set(key, value):
        return f'set {key} = {html.escape(value)}'
    return _unknown(key)


def _row(key: str, value: str, note: str) -> str:
    """One HTML table row: key, current value, one-line help (escaped)."""
    return (
        f'<tr><td><code>{key}</code></td>'
        f'<td>{html.escape(value) or "-"}</td>'
        f'<td>{html.escape(note)}</td></tr>'
    )


@dataclass(frozen=True)
class _Moderator:
    """The admin panel: backend, on-demand clean, settings and status."""

    cfg: Settings
    toggle: BackendToggle
    api: TgApi
    log: logging.Logger

    def __call__(self, text: str) -> str:
        """Route a command: a verb with an argument, else a single word."""
        parts = text.split()
        if len(parts) >= _VERB_PARTS and parts[0].lower() in _VERBS:
            return self._verb(parts[0].lower(), parts[1], parts[2:])
        return self._word(text.strip().lower(), text)

    def _verb(self, verb: str, key: str, rest: list[str]) -> str:
        """A two-part command: set/get/reset a setting, or whois a chat."""
        if verb == 'whois':
            return self._whois(key)
        admin = admin_config(self.cfg.state)
        if verb == 'get':
            if key not in KEYS:
                return _unknown(key)
            return f'{key} = {html.escape(admin.get(key))}'
        if verb == 'reset':
            return f'reset {key}' if admin.reset(key) else _unknown(key)
        return _set_reply(admin, key, ' '.join(rest))

    def _word(self, word: str, text: str) -> str:
        """A single-word command, or the backend fallback."""
        action = self._actions().get(word)
        return action() if action else reply_for(self.toggle, text)

    def _actions(self) -> dict[str, Callable[[], str]]:
        """Every single-word command mapped to its handler."""
        panel = lambda: _MENU  # noqa: E731 -- terse alias for the menu
        return {
            'menu': panel,
            'help': panel,
            'panel': panel,
            'admin': panel,
            'config': self._config,
            'settings': self._config,
            'bed': self._bed,
            'wishlist': self._wishlist,
            'clean': self._clean,
            'cleanup': self._clean,
        }

    def _config(self) -> str:
        """Every setting as an HTML table -- edit it like a table."""
        admin = admin_config(self.cfg.state)
        rows = ''.join(_row(s.key, admin.get(s.key), s.help) for s in SETTINGS)
        return (
            'edit with <code>set [key] [value]</code> or '
            '<code>reset [key]</code>\n'
            '<table><tr><th>key</th><th>value</th><th>what</th></tr>'
            + rows
            + '</table>'
        )

    def _whois(self, chat: str) -> str:
        """Resolve a chat id to its name via the Bot API."""
        who = html.escape(chat)
        name = chat_title(self.api, chat)
        if name:
            return f'{who} -> {html.escape(name)}'
        return f'{who}: unknown / no access'

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
        roll = ', '.join(html.escape(name or 'anon') for name in names)
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
        parse_mode='HTML',
    )
    log = bot_logger(BOT, cfg.logs)
    handle = _Moderator(cfg, BackendToggle(cfg), api, log)
    return TgCommands(api, spec, handle)


def main(env: Mapping[str, str] | None = None) -> int:
    """Build Settings once, service commands forever (tokenless: idle)."""
    mapping = os.environ if env is None else env
    cfg = load(mapping)
    return run(BOT, build(cfg, mapping), cfg.logs)


if __name__ == '__main__':
    raise SystemExit(main())
