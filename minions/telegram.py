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
belt on its own thread (one getUpdates consumer per token).
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

from minion_core.kernel import run
from minion_core.settings import load
from minions.relay.main import build

if TYPE_CHECKING:
    from collections.abc import Mapping

    from minion_core.settings import Settings


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


def _belt(
    cfg: Settings, base: Mapping[str, str], name: str
) -> threading.Thread:
    """Build one bot's relay belt and its draining thread."""
    env = _bot_env(base, name)
    (cfg.bot_dir(name) / '_spool').mkdir(parents=True, exist_ok=True)
    graph = build(cfg, env)
    return threading.Thread(target=run, args=(name, graph, cfg.logs))


def main(env: Mapping[str, str] | None = None) -> int:
    """Run one relay belt per configured bot; block until all drain."""
    base = os.environ if env is None else env
    cfg = load(base)
    threads = [_belt(cfg, base, name) for name in _names(base)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
