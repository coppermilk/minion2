"""frames bot: a video (file or link) becomes a folder of frames.

Graph: (TgAny | Folder) -> FetchLink -> ExtractFrames -> Reply ->
DisposeSource. ExtractFrames writes every 5th frame into
``done/<MMDD> <clip name>/`` and files the clip in that folder's
``_done/``; the chat gets a one-line summary, never the frames.
FetchLink passes media files through untouched, so links, chat
videos and watched-folder videos share one belt (REQ-DOCK-001).
"""

from __future__ import annotations

import functools
import os
from typing import TYPE_CHECKING

from minion_core.adapters.fetch import FetchLink
from minion_core.adapters.files import free_quota
from minion_core.adapters.frames import ExtractFrames
from minion_core.adapters.tg import SpoolSpec
from minion_core.adapters.tg import TgAny
from minion_core.adapters.tg import TgApi
from minion_core.adapters.tg import TgChannel
from minion_core.adapters.tg import TgSpec
from minion_core.adapters.tg import chats_from
from minion_core.adapters.tg import spool_of
from minion_core.kernel import DisposeSource
from minion_core.kernel import FolderSpec
from minion_core.kernel import Reply
from minion_core.kernel import SeenPaths
from minion_core.kernel import merge_watch
from minion_core.kernel import run
from minion_core.settings import load

if TYPE_CHECKING:
    from collections.abc import Mapping

    from minion_core.kernel import Stage
    from minion_core.settings import Settings

BOT = 'frames'

VIDEO_EXTS = ('.mp4', '.mkv', '.webm', '.mov', '.avi')
"""Video suffixes the watch dock accepts."""


def build(cfg: Settings, env: Mapping[str, str]) -> Stage:
    """Assemble the belt; secrets come from the passed mapping.

    One token allows one getUpdates consumer, so links and chat
    videos share the TgAny dock. A folder dock is always present --
    drop a video into the bot's own dir and it is processed;
    ``FRAMES_WATCH`` overrides the watched dir (REQ-DOCK-001).
    """
    api = TgApi(env.get('TG_TOKEN', ''))
    # Telegram/link downloads live in _spool (a subfolder), so the drop
    # watcher over the bot's own folder never re-globs them.
    spool = cfg.bot_dir(BOT) / '_spool'
    spec = TgSpec(
        spool=SpoolSpec(into=spool, budget=functools.partial(free_quota, cfg)),
        dest=spool,
        offset=cfg.state / f'{BOT}.offset',
        chats=chats_from(env),
        help='Send or drop a video (or a link) and I extract frames.',
    )
    channel = TgChannel(api)
    # Default drop folder is the bot's own dir; FRAMES_WATCH overrides.
    watch = FolderSpec(
        root=cfg.frames_watch or cfg.bot_dir(BOT),
        dest=spool,
        exts=VIDEO_EXTS,
        poll_sec=cfg.poll_sec,
    )
    docks = merge_watch(TgAny(api, spec), watch, SeenPaths(cfg.seen_paths_max))
    return (
        docks
        >> FetchLink(cfg)
        >> ExtractFrames(cfg)
        >> Reply(channel)
        >> DisposeSource(spool_of)
    )


def main(env: Mapping[str, str] | None = None) -> int:
    """Build Settings once and drain the belt."""
    mapping = os.environ if env is None else env
    cfg = load(mapping)
    (cfg.bot_dir(BOT) / '_spool').mkdir(parents=True, exist_ok=True)
    return run(BOT, build(cfg, mapping), cfg.logs)


if __name__ == '__main__':
    raise SystemExit(main())
