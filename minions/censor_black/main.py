"""censor-black bot: detected faces are blacked out.

Graph: (TgMedia | Folder) -> HideFaces -> RouteOrigin(chat / done
dir) -> Reply -> DisposeSource. Faces, not the whole person, so a
portrait keeps its scene. One of the three censor-family bots
(BLUEPRINT 9 waiver): one Telegram identity per behaviour;
``CENSOR_BLACK_WATCH`` adds the local dock (REQ-DOCK-001).
"""

from __future__ import annotations

import functools
import os
from typing import TYPE_CHECKING

from minion_core.adapters.files import free_quota
from minion_core.adapters.tg import SpoolSpec
from minion_core.adapters.tg import TgApi
from minion_core.adapters.tg import TgChannel
from minion_core.adapters.tg import TgMedia
from minion_core.adapters.tg import TgSpec
from minion_core.adapters.tg import chats_from
from minion_core.adapters.tg import spool_of
from minion_core.adapters.vision import IMAGE_EXTS
from minion_core.adapters.vision import HideFaces
from minion_core.adapters.vision import warm_faces
from minion_core.kernel import ArchiveTo
from minion_core.kernel import DisposeSource
from minion_core.kernel import FolderSpec
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
    spec = TgSpec(
        spool=SpoolSpec(
            into=cfg.bot_dir(BOT), budget=functools.partial(free_quota, cfg)
        ),
        dest=cfg.bot_dir(BOT),
        offset=cfg.state / f'{BOT}.offset',
        chats=chats_from(env),
        help='Send a photo and I black out faces.',
    )
    channel = TgChannel(api)
    watch = None
    if cfg.censor_black_watch is not None:
        watch = FolderSpec(
            root=cfg.censor_black_watch,
            dest=cfg.bot_dir(BOT),
            exts=IMAGE_EXTS,
            poll_sec=cfg.poll_sec,
        )
    docks = merge_watch(
        TgMedia(api, spec), watch, SeenPaths(cfg.seen_paths_max)
    )
    route = RouteOrigin(
        tg=SendResult(channel), loc=ArchiveTo(cfg.bot_done(BOT))
    )
    return (
        docks
        >> HideFaces()
        >> route
        >> Reply(channel)
        >> DisposeSource(spool_of)
    )


def main(env: Mapping[str, str] | None = None) -> int:
    """Build Settings once, warm the model at init, drain the belt."""
    mapping = os.environ if env is None else env
    cfg = load(mapping)
    if mapping.get('TG_TOKEN') or cfg.censor_black_watch is not None:
        warm_faces()  # resources at init, never mid-flight
    return run(BOT, build(cfg, mapping), cfg.logs)


if __name__ == '__main__':
    raise SystemExit(main())
