"""restore bot: blur the people, then the LLM repaints the scene.

Graph: (TgMedia | Folder) -> HidePersonBoxes -> RestoreBackground
-> RouteOrigin(chat / nothing) -> Reply -> Shelve. A
two-step belt: HidePersonBoxes writes the ``_s1`` intermediate, the LLM
repaint delivers ``_s2`` (OPERATIONS 6). One of the three
censor-family bots (BLUEPRINT 9 waiver). A folder dock is always
present -- drop a photo into the bot's own dir and it is processed;
``RESTORE_WATCH`` overrides the watched dir (REQ-DOCK-001).
"""

from __future__ import annotations

import functools
import os
from typing import TYPE_CHECKING

from minion_core.adapters.files import Shelve
from minion_core.adapters.files import free_quota
from minion_core.adapters.llm import RestoreBackground
from minion_core.adapters.llm import spec_from
from minion_core.adapters.tg import SpoolSpec
from minion_core.adapters.tg import TgApi
from minion_core.adapters.tg import TgChannel
from minion_core.adapters.tg import TgMedia
from minion_core.adapters.tg import TgSpec
from minion_core.adapters.tg import chats_from
from minion_core.adapters.tg import spooled_or_dropped
from minion_core.adapters.vision import IMAGE_EXTS
from minion_core.adapters.vision import HidePersonBoxes
from minion_core.adapters.vision import warm_detector
from minion_core.kernel import FolderSpec
from minion_core.kernel import Null
from minion_core.kernel import Reply
from minion_core.kernel import RouteOrigin
from minion_core.kernel import SeenPaths
from minion_core.kernel import SendResult
from minion_core.kernel import merge_watch
from minion_core.kernel import run
from minion_core.settings import load

if TYPE_CHECKING:
    from collections.abc import Mapping

    from minion_core.kernel import Stage
    from minion_core.settings import Settings

BOT = 'restore'


def build(cfg: Settings, env: Mapping[str, str]) -> Stage:
    """Assemble the belt; secrets come from the passed mapping."""
    api = TgApi(env.get('TG_TOKEN', ''))
    # Telegram downloads and _s1/_s2 files live in _spool (a subfolder),
    # so the drop watcher over the bot's own folder never re-globs them.
    spool = cfg.bot_dir(BOT) / '_spool'
    spec = TgSpec(
        spool=SpoolSpec(into=spool, budget=functools.partial(free_quota, cfg)),
        dest=spool,
        offset=cfg.state / f'{BOT}.offset',
        chats=chats_from(env),
        help='Send or drop a photo and I repaint the scene without people.',
    )
    channel = TgChannel(api)
    # Default drop folder is the bot's own dir; RESTORE_WATCH overrides.
    watch = FolderSpec(
        root=cfg.restore_watch or cfg.bot_dir(BOT),
        dest=spool,
        exts=IMAGE_EXTS,
        poll_sec=cfg.poll_sec,
    )
    docks = merge_watch(
        TgMedia(api, spec), watch, SeenPaths(cfg.seen_paths_max)
    )
    route = RouteOrigin(tg=SendResult(channel), loc=Null())
    return (
        docks
        >> HidePersonBoxes()
        >> RestoreBackground(spec_from(env))
        >> route
        >> Reply(channel)
        >> Shelve(cfg.bot_done(BOT), spooled_or_dropped)
    )


def main(env: Mapping[str, str] | None = None) -> int:
    """Build Settings once, warm the model at init, drain the belt."""
    mapping = os.environ if env is None else env
    cfg = load(mapping)
    (cfg.bot_dir(BOT) / '_spool').mkdir(parents=True, exist_ok=True)
    warm_detector()  # a folder dock is always present; resources at init
    return run(BOT, build(cfg, mapping), cfg.logs)


if __name__ == '__main__':
    raise SystemExit(main())
