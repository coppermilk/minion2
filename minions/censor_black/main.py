"""censor-black bot: detected faces are blacked out.

Graph: (TgMedia | Folder) -> HideFaces -> RouteOrigin(chat /
nothing) -> Reply -> Shelve. Faces, not the whole person, so a
portrait keeps its scene. One of the three censor-family bots
(BLUEPRINT 9 waiver): one Telegram identity per behaviour. A folder
dock is always present -- drop a photo into the bot's own dir and it
is processed; ``CENSOR_BLACK_WATCH`` overrides the watched dir
(REQ-DOCK-001).
"""

from __future__ import annotations

import functools
import os
from typing import TYPE_CHECKING

from minion_core.adapters.files import Shelve
from minion_core.adapters.files import free_quota
from minion_core.adapters.tg import SpoolSpec
from minion_core.adapters.tg import TgApi
from minion_core.adapters.tg import TgChannel
from minion_core.adapters.tg import TgMedia
from minion_core.adapters.tg import TgSpec
from minion_core.adapters.tg import chats_from
from minion_core.adapters.tg import spooled_or_dropped
from minion_core.adapters.vision import IMAGE_EXTS
from minion_core.adapters.vision import HideFaces
from minion_core.adapters.vision import warm_faces
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

BOT = 'censor-black'


def build(cfg: Settings, env: Mapping[str, str]) -> Stage:
    """Assemble the belt; secrets come from the passed mapping."""
    api = TgApi(env.get('TG_TOKEN', ''))
    # Telegram downloads and _s1 results live in _spool (a subfolder),
    # so the drop watcher over the bot's own folder never re-globs them.
    spool = cfg.bot_dir(BOT) / '_spool'
    spec = TgSpec(
        spool=SpoolSpec(into=spool, budget=functools.partial(free_quota, cfg)),
        dest=spool,
        offset=cfg.state / f'{BOT}.offset',
        chats=chats_from(env),
        help='Send or drop a photo and I black out faces.',
    )
    channel = TgChannel(api)
    # Default drop folder is the bot's own dir; CENSOR_BLACK_WATCH overrides.
    watch = FolderSpec(
        root=cfg.censor_black_watch or cfg.bot_dir(BOT),
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
        >> HideFaces()
        >> route
        >> Reply(channel)
        >> Shelve(cfg.bot_done(BOT), spooled_or_dropped)
    )


def main(env: Mapping[str, str] | None = None) -> int:
    """Build Settings once, warm the model at init, drain the belt."""
    mapping = os.environ if env is None else env
    cfg = load(mapping)
    (cfg.bot_dir(BOT) / '_spool').mkdir(parents=True, exist_ok=True)
    warm_faces()  # a folder dock is always present; resources at init
    return run(BOT, build(cfg, mapping), cfg.logs)


if __name__ == '__main__':
    raise SystemExit(main())
