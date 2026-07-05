"""model-switch bot: flip the classification/props backend from chat.

A control bot, not a belt. It answers text commands (``local``,
``gemini``, ``status``) by flipping the STATE toggle that sort and
props read, so the whole system swaps model with no restart. Restore
is unaffected (Gemini-only image generation).
"""

from __future__ import annotations

import functools
import os
from typing import TYPE_CHECKING

from minion_core.adapters.backend import CHOICES
from minion_core.adapters.backend import GEMINI
from minion_core.adapters.backend import LOCAL
from minion_core.adapters.backend import BackendToggle
from minion_core.adapters.files import free_quota
from minion_core.adapters.tg import SpoolSpec
from minion_core.adapters.tg import TgApi
from minion_core.adapters.tg import TgCommands
from minion_core.adapters.tg import TgSpec
from minion_core.adapters.tg import chats_from
from minion_core.kernel import run
from minion_core.settings import load

if TYPE_CHECKING:
    from collections.abc import Mapping

    from minion_core.kernel import Stage
    from minion_core.settings import Settings

BOT = 'model-switch'

_STATUS = ('status', 'which', '')


def reply_for(toggle: BackendToggle, text: str) -> str:
    """Map a command to a reply, flipping the toggle on a valid word."""
    word = text.strip().lower()
    if word in CHOICES:
        toggle.write(word)
        return f'backend set to {word}'
    if word in _STATUS:
        return f'backend is {toggle.read()}'
    return f'send one of: {LOCAL}, {GEMINI}, status'


def build(cfg: Settings, env: Mapping[str, str]) -> Stage:
    """Assemble the command dock; the toggle is bound into the handler."""
    api = TgApi(env.get('TG_TOKEN', ''))
    spec = TgSpec(
        spool=SpoolSpec(
            into=cfg.bot_dir(BOT), budget=functools.partial(free_quota, cfg)
        ),
        dest=cfg.bot_dir(BOT),
        offset=cfg.state / f'{BOT}.offset',
        chats=chats_from(env),
    )
    handle = functools.partial(reply_for, BackendToggle(cfg))
    return TgCommands(api, spec, handle)


def main(env: Mapping[str, str] | None = None) -> int:
    """Build Settings once, service commands forever (tokenless: idle)."""
    mapping = os.environ if env is None else env
    cfg = load(mapping)
    return run(BOT, build(cfg, mapping), cfg.logs)


if __name__ == '__main__':
    raise SystemExit(main())
