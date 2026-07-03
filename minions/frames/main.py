"""frames bot: a video (file or link) becomes its every-Nth frames.

Graph: (TgAny | Folder) -> FetchLink -> ExtractFrames ->
RouteOrigin(chat / done dir) -> Reply -> DisposeSource. FetchLink
passes media files through untouched, so links, chat videos and
watched-folder videos share one belt (REQ-DOCK-001).
"""

from __future__ import annotations

import functools
import os
from typing import TYPE_CHECKING

from minion_core.adapters import video
from minion_core.adapters.fetch import FetchLink
from minion_core.adapters.files import free_quota
from minion_core.adapters.files import sanitize
from minion_core.adapters.tg import SpoolSpec
from minion_core.adapters.tg import TgAny
from minion_core.adapters.tg import TgApi
from minion_core.adapters.tg import TgChannel
from minion_core.adapters.tg import TgSpec
from minion_core.adapters.tg import chats_from
from minion_core.adapters.tg import spool_of
from minion_core.kernel import ArchiveTo
from minion_core.kernel import DisposeSource
from minion_core.kernel import Disposition
from minion_core.kernel import FolderSpec
from minion_core.kernel import Reply
from minion_core.kernel import RouteOrigin
from minion_core.kernel import SeenPaths
from minion_core.kernel import SendResult
from minion_core.kernel import Step
from minion_core.kernel import Verdict
from minion_core.kernel import merge_watch
from minion_core.kernel import run
from minion_core.settings import load

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from minion_core.kernel import Job
    from minion_core.kernel import Stage
    from minion_core.settings import Settings

BOT = 'frames'

VIDEO_EXTS = ('.mp4', '.mkv', '.webm', '.mov', '.avi')
"""Video suffixes the watch dock accepts."""

STRIDE = 5
"""Every 5th source frame -- an invariant, deliberately not a knob
(the timecoded file names below encode this step)."""


def _timecode(total_sec: int, frame_no: int) -> str:
    """``[hour-]minute-second-frame``: 1-05-325 or 1-1-05-198000.

    The hour field appears only for videos longer than an hour; the
    trailing number is the source frame index (a multiple of 5).
    """
    hours, rest = divmod(total_sec, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f'{hours}-{minutes}-{seconds:02d}-{frame_no}'
    return f'{minutes}-{seconds:02d}-{frame_no}'


def _stamp(shots: list[Path], fps: float, label: str) -> list[Path]:
    """Rename ffmpeg's sequence to ``<timecode>_<video name>`` names.

    The label rides along so a frame stays recognizable outside its
    folder (a downloaded video's stem is its title).
    """
    named = []
    for k, shot in enumerate(shots):
        frame_no = k * STRIDE
        code = _timecode(int(frame_no / fps), frame_no)
        named.append(shot.rename(shot.with_name(f'{code}_{label}.jpg')))
    return named


class ExtractFrames(Step):
    """The bot's one transformation: video -> timecoded frames."""

    def __init__(self, cfg: Settings) -> None:
        self._cfg = cfg

    def process(self, job: Job) -> Verdict:
        """Extract every 5th frame; timecode + video name each."""
        out = job.dest / f'{job.src.stem}_frames'
        spec = video.FrameSpec(
            stride=STRIDE,
            timeout_sec=self._cfg.download_timeout_sec,
        )
        try:
            fps = video.probe_fps(job.src, spec.timeout_sec)
            shots = video.frames(job.src, out, spec)
        except video.ProbeError:
            return Verdict(Disposition.FAILED, reason='probe_failed')
        if not shots:
            return Verdict(Disposition.FAILED, reason='probe_failed')
        named = _stamp(shots, fps, sanitize(job.src.stem))
        return Verdict(
            Disposition.DELIVERED, result=out, reply=f'{len(named)} frames'
        )


def build(cfg: Settings, env: Mapping[str, str]) -> Stage:
    """Assemble the belt; secrets come from the passed mapping.

    One token allows one getUpdates consumer, so links and chat
    videos share the TgAny dock; ``frames_watch`` adds the local
    dock (REQ-DOCK-001).
    """
    api = TgApi(env.get('TG_TOKEN', ''))
    spec = TgSpec(
        spool=SpoolSpec(
            into=cfg.bot_dir(BOT), budget=functools.partial(free_quota, cfg)
        ),
        dest=cfg.bot_dir(BOT),
        offset=cfg.state / f'{BOT}.offset',
        chats=chats_from(env),
    )
    channel = TgChannel(api)
    watch = None
    if cfg.frames_watch is not None:
        watch = FolderSpec(
            root=cfg.frames_watch,
            dest=cfg.bot_dir(BOT),
            exts=VIDEO_EXTS,
            poll_sec=cfg.poll_sec,
        )
    docks = merge_watch(TgAny(api, spec), watch, SeenPaths(cfg.seen_paths_max))
    route = RouteOrigin(
        tg=SendResult(channel), loc=ArchiveTo(cfg.bot_done(BOT))
    )
    return (
        docks
        >> FetchLink(cfg)
        >> ExtractFrames(cfg)
        >> route
        >> Reply(channel)
        >> DisposeSource(spool_of)
    )


def main(env: Mapping[str, str] | None = None) -> int:
    """Build Settings once and drain the belt."""
    mapping = os.environ if env is None else env
    cfg = load(mapping)
    return run(BOT, build(cfg, mapping), cfg.logs)


if __name__ == '__main__':
    raise SystemExit(main())
