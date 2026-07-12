"""frames bot tests: the hardcoded stride and timecode naming."""

from __future__ import annotations

from typing import TYPE_CHECKING

from minion_core.adapters import video
from minion_core.kernel import Disposition
from minion_core.kernel import Job
from minion_core.kernel import Origin
from minions.frames.step import STRIDE
from minions.frames.step import ExtractFrames
from minions.frames.step import _timecode
from tests.conftest import make_cfg

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_stride_is_hardcoded_to_five() -> None:
    """Every 5th frame -- an invariant, deliberately not a knob."""
    assert STRIDE == 5


def test_timecode_omits_empty_leading_fields() -> None:
    """Only the fields that exist: no zero-filled hours or minutes."""
    assert _timecode(0, 0) == '0-0'  # 0s, frame 0
    assert _timecode(25, 5) == '25-5'  # under a minute: S-frame
    assert _timecode(65, 325) == '1-05-325'  # over a minute: M-SS-frame
    assert _timecode(3661, 198000) == '1-01-01-198000'  # over an hour


def test_extract_builds_folder_with_done(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frames land in <MMDD> <clip>/; the clip is filed in _done/."""
    cfg = make_cfg(tmp_path / 'drive')
    clip = cfg.bot_dir('frames') / 'clip.mp4'
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b'video')

    def fake_frames(src: Path, out: Path, spec: video.FrameSpec) -> list[Path]:
        assert spec.stride == STRIDE
        out.mkdir(parents=True, exist_ok=True)
        shots = [out / f'frame_{i:04d}.jpg' for i in range(1, 4)]
        for shot in shots:
            shot.write_bytes(b'jpg')
        return shots

    monkeypatch.setattr(video, 'frames', fake_frames)
    monkeypatch.setattr(video, 'probe_fps', lambda p, t: 5.0)
    job = Job(
        src=clip,
        dest=clip.parent,
        stem='clip',
        origin=Origin('tg', f'1:2:{clip}'),
    )
    verdict = ExtractFrames(cfg).process(job)
    assert verdict.disposition is Disposition.DELIVERED
    assert verdict.result is not None
    folder = verdict.result
    assert folder.parent == cfg.bot_done('frames')
    assert folder.name.endswith('clip')  # MMDD clip
    frames = sorted(p.name for p in folder.iterdir() if p.is_file())
    # fps=5: frame 0@0s, frame 5@1s, frame 10@2s; compact, name rides
    assert frames == ['0-0_clip.jpg', '1-5_clip.jpg', '2-10_clip.jpg']
    assert (folder / '_done' / 'clip.mp4').is_file()  # clip shelved
    assert not clip.exists()  # moved out of the work dir
    assert verdict.reply is not None
    assert 'frames' in verdict.reply  # summary, not the files


def test_missing_fps_is_probe_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unprobeable video maps to the stable reason code."""
    cfg = make_cfg(tmp_path / 'drive')
    clip = cfg.bot_dir('frames') / 'clip.mp4'
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b'video')

    def refuse(path: Path, timeout: int) -> float:
        raise video.ProbeError('probe_failed: clip.mp4')

    monkeypatch.setattr(video, 'probe_fps', refuse)
    job = Job(
        src=clip,
        dest=clip.parent,
        stem='clip',
        origin=Origin('tg', f'1:2:{clip}'),
    )
    verdict = ExtractFrames(cfg).process(job)
    assert verdict.disposition is Disposition.FAILED
    assert verdict.reason == 'probe_failed'
