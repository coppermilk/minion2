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

from minion_core.adapters import video
from minion_core.adapters.fetch import FetchLink
from minion_core.adapters.files import dated_dir
from minion_core.adapters.files import free_quota
from minion_core.adapters.files import move_atomic
from minion_core.adapters.files import next_free_path
from minion_core.adapters.files import sanitize
from minion_core.adapters.tg import SpoolSpec
from minion_core.adapters.tg import TgAny
from minion_core.adapters.tg import TgApi
from minion_core.adapters.tg import TgChannel
from minion_core.adapters.tg import TgSpec
from minion_core.adapters.tg import chats_from
from minion_core.adapters.tg import spool_of
from minion_core.kernel import DisposeSource
from minion_core.kernel import Disposition
from minion_core.kernel import FolderSpec
from minion_core.kernel import Reply
from minion_core.kernel import SeenPaths
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
    """``[H-][MM-]S-frame`` -- only the fields that exist.

    A short clip has no hours and maybe no minutes, so those fields
    are omitted rather than zero-filled: ``5-25`` (25th frame at 5s),
    ``1-05-325`` past a minute, ``1-01-05-3900`` past an hour. The
    trailing number is the source frame index (a multiple of 5).
    """
    hours, rest = divmod(total_sec, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        fields = [str(hours), f'{minutes:02d}', f'{seconds:02d}']
    elif minutes:
        fields = [str(minutes), f'{seconds:02d}']
    else:
        fields = [str(seconds)]
    fields.append(str(frame_no))
    return '-'.join(fields)


def _stamp(shots: list[Path], fps: float, label: str) -> list[Path]:
    """Rename the sequence (already in the folder) to timecode names."""
    named = []
    for k, shot in enumerate(shots):
        frame_no = k * STRIDE
        code = _timecode(int(frame_no / fps), frame_no)
        dest = next_free_path(shot.with_name(f'{code}_{label}.jpg'))
        named.append(shot.rename(dest))
    return named


class ExtractFrames(Step):
    """video -> a per-clip folder of frames, the clip filed in _done.

    Every 5th frame lands in ``done/<MMDD> <clip name>/``; the clip
    itself moves into that folder's ``_done/`` after processing. The
    result is the folder (never the individual frames), so the belt
    replies a one-line summary instead of flooding the chat.
    """

    def __init__(self, cfg: Settings) -> None:
        self._cfg = cfg

    def process(self, job: Job) -> Verdict:
        """Extract, name by timecode, shelve the clip in _done."""
        spec = video.FrameSpec(
            stride=STRIDE,
            timeout_sec=self._cfg.download_timeout_sec,
        )
        folder = next_free_path(
            self._cfg.bot_done(BOT) / dated_dir(job.src.stem)
        )
        folder.mkdir(parents=True, exist_ok=True)
        try:
            fps = video.probe_fps(job.src, spec.timeout_sec)
            shots = video.frames(job.src, folder, spec)
        except video.ProbeError:
            return Verdict(Disposition.FAILED, reason='probe_failed')
        if not shots:
            return Verdict(Disposition.FAILED, reason='probe_failed')
        named = _stamp(shots, fps, sanitize(job.src.stem))
        move_atomic(job.src, next_free_path(folder / '_done' / job.src.name))
        return Verdict(
            Disposition.DELIVERED,
            result=folder,
            reply=f'{len(named)} frames -> {folder.name}',
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
        help='Send a video or a link and I extract frames.',
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
    return run(BOT, build(cfg, mapping), cfg.logs)


if __name__ == '__main__':
    raise SystemExit(main())
