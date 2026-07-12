"""One Telegram container: all bot docks, each calling a web service.

    python -m minions.telegram

Total separation: no processor IP lives here. Every belt is a Telegram dock
that POSTs the file to its service over HTTP (``CallService``) and sends the
bytes back. The Telegram identities all live in this one clean container; the
blur/frames/... code lives in the ``svc-*`` services it talks to over the web.

A top-level module (not a bot package), so it may compose the relay belt
(``minions.relay``) the way ``minions/service.py`` names the Steps -- the
import-direction rule only fences bot-to-bot imports (REQ-ARC-001).

Config (env): ``TELEGRAM_BOTS`` is a csv of bot names; for each name ``X``
(upper-cased, ``-`` -> ``_``) it reads ``TG_TOKEN_X``, ``SERVICE_URL_X`` and
optional ``RELAY_DOCK_X`` (media | any | links). Each bot runs its own relay
belt on its own thread, under a supervisor: a belt that exits (a crashed
getUpdates, a fatal init) is rebuilt and re-run with exponential backoff, so
one bot's failure never leaves it dead until the whole container restarts.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from minion_core.kernel import run
from minion_core.settings import load
from minions.relay.main import build

if TYPE_CHECKING:
    from collections.abc import Mapping

    from minion_core.settings import Settings

_BACKOFF_START_SEC = 2.0
"""First restart delay after a belt exits."""

_BACKOFF_MAX_SEC = 60.0
"""Cap on the restart delay (a crash loop backs off no further)."""

_HEALTHY_RUN_SEC = 60.0
"""A belt that ran at least this long is healthy; its backoff resets."""


@dataclass(frozen=True)
class _Bot:
    """One supervised belt: its Settings, name, and resolved env."""

    cfg: Settings
    name: str
    env: Mapping[str, str]


def _names(env: Mapping[str, str]) -> list[str]:
    """The configured bot names (csv in TELEGRAM_BOTS)."""
    raw = env.get('TELEGRAM_BOTS', '')
    return [n.strip() for n in raw.split(',') if n.strip()]


def _bot_env(base: Mapping[str, str], name: str) -> dict[str, str]:
    """Per-bot env: its own token, service URL, dock and relay name."""
    key = name.upper().replace('-', '_')
    env = dict(base)
    env['TG_TOKEN'] = base.get(f'TG_TOKEN_{key}', '')
    env['SERVICE_URL'] = base.get(f'SERVICE_URL_{key}', '')
    env['RELAY_DOCK'] = base.get(f'RELAY_DOCK_{key}', 'media')
    env['RELAY_NAME'] = name
    return env


def _run_once(bot: _Bot, log: logging.Logger) -> None:
    """Build and drain one belt; a crash is logged, never raised."""
    (bot.cfg.bot_dir(bot.name) / '_spool').mkdir(parents=True, exist_ok=True)
    try:
        run(bot.name, build(bot.cfg, bot.env), bot.cfg.logs)
    except Exception:
        log.exception('belt_crashed')


def _next_delay(delay: float, ran_sec: float) -> float:
    """Reset the backoff after a healthy run, else grow it up to the cap."""
    if ran_sec >= _HEALTHY_RUN_SEC:
        return _BACKOFF_START_SEC
    return min(delay * 2.0, _BACKOFF_MAX_SEC)


def _supervise(bot: _Bot, stop: threading.Event) -> None:
    """Keep one bot's belt alive: rebuild and re-run it with backoff."""
    delay = _BACKOFF_START_SEC
    log = logging.getLogger(bot.name)
    while not stop.is_set():
        started = time.monotonic()
        _run_once(bot, log)
        if stop.is_set():
            return
        log.warning('belt_exited restart_in=%.0fs', delay)
        stop.wait(delay)
        delay = _next_delay(delay, time.monotonic() - started)


def main(env: Mapping[str, str] | None = None) -> int:
    """Run one supervised relay belt per configured bot; block forever."""
    base = os.environ if env is None else env
    cfg = load(base)
    stop = threading.Event()
    bots = [_Bot(cfg, name, _bot_env(base, name)) for name in _names(base)]
    threads = [
        threading.Thread(target=_supervise, args=(bot, stop)) for bot in bots
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
