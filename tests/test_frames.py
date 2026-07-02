"""frames bot tests: the hardcoded stride and timecode naming."""

from __future__ import annotations

from typing import TYPE_CHECKING

from minion_core.adapters import video
from minion_core.kernel import Disposition
from minion_core.kernel import Job
from minion_core.kernel import Origin
from minions.frames.main import STRIDE
from minions.frames.main import ExtractFrames
from minions.frames.main import _timecode
from tests.conftest import make_cfg

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_stride_is_hardcoded_to_five() -> None:
    """Every 5th frame -- an invariant, deliberately not a knob."""
    assert STRIDE == 5


def test_timecode_under_an_hour() -> None:
    """minute-second-frame, seconds zero-padded."""
    assert _timecode(0, 0) == '0-00-0'
    assert _timecode(65, 325) == '1-05-325'
    assert _timecode(59, 295) == '0-59-295'


def test_timecode_past_the_hour() -> None:
    """The hour field appears only for videos over an hour."""
    assert _timecode(3661, 198000) == '1-1-01-198000'
    assert _timecode(7325, 900000) == '2-2-05-900000'


def test_extract_names_frames_by_timecode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ffmpeg sequence becomes [hour-]min-sec-frame names."""
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
    names = sorted(p.name for p in verdict.result.iterdir())
    # fps=5: frames 0, 5, 10 land at 0s, 1s, 2s
    assert names == ['0-00-0.jpg', '0-01-5.jpg', '0-02-10.jpg']


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
