"""Frame extraction Step: a video -> a per-clip folder of frames.

Telegram-free (like every processing adapter): it knows nothing about where
the video came from, so it runs inside a service (``svc-frames``) with no
transport code. The frames bot and the service both take ``ExtractFrames``
from here; only the bot's dock/sinks live in ``minions/frames``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from minion_core.adapters import video
from minion_core.adapters.files import dated_dir
from minion_core.adapters.files import move_atomic
from minion_core.adapters.files import next_free_path
from minion_core.adapters.files import sanitize
from minion_core.kernel import Disposition
from minion_core.kernel import Step
from minion_core.kernel import Verdict

if TYPE_CHECKING:
    from pathlib import Path

    from minion_core.kernel import Job
    from minion_core.settings import Settings

_DONE = 'frames'
"""The bot_done subtree the clips and frames land under."""

STRIDE = 5
"""Every 5th source frame -- an invariant, deliberately not a knob
(the timecoded file names below encode this step)."""


def _timecode(total_sec: int, frame_no: int) -> str:
    """``[H-][MM-]S-frame`` -- only the fields that exist.

    A short clip has no hours and maybe no minutes, so those fields are
    omitted rather than zero-filled: ``5-25`` (25th frame at 5s),
    ``1-05-325`` past a minute, ``1-01-05-3900`` past an hour. The trailing
    number is the source frame index (a multiple of 5).
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

    Every 5th frame lands in ``done/<MMDD> <clip name>/``; the clip itself
    moves into that folder's ``_done/`` after processing. The result is the
    folder (never the individual frames), so the caller gets a one-line
    summary rather than a flood of files.
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
            self._cfg.bot_done(_DONE) / dated_dir(job.src.stem)
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
