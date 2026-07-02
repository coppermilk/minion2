"""Video boundary: probe + frame extraction (ffmpeg/ffprobe).

Owns the ffmpeg binaries (subprocess, no SDK). Every invocation is
wall-time bounded; a missing binary or a failed probe is the stable
reason code ``probe_failed`` (OPERATIONS 2).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

FFPROBE = 'ffprobe'
FFMPEG = 'ffmpeg'
"""Pinned binaries; the image apt-installs them (OPERATIONS 2)."""


class ProbeError(Exception):
    """ffmpeg/ffprobe missing or refused; reason ``probe_failed``."""


@dataclass(frozen=True)
class FrameSpec:
    """Extraction knobs: keep every Nth frame, bounded in time."""

    stride: int
    timeout_sec: int


def probe(path: Path, timeout_sec: int) -> float:
    """Duration in seconds, via ffprobe."""
    argv = [FFPROBE, '-v', 'error', '-print_format', 'json',
            '-show_format', str(path)]
    out = _run(argv, timeout_sec)
    try:
        return float(json.loads(out)['format']['duration'])
    except (KeyError, ValueError) as exc:
        raise ProbeError(f'probe_failed: {path.name}') from exc


def frames(src: Path, out: Path, spec: FrameSpec) -> list[Path]:
    """Extract every ``stride``-th frame into ``out`` as JPEGs."""
    out.mkdir(parents=True, exist_ok=True)
    select = f'select=not(mod(n\\,{spec.stride}))'
    argv = [FFMPEG, '-y', '-i', str(src), '-vf', select,
            '-vsync', 'vfr', str(out / 'frame_%04d.jpg')]
    _run(argv, spec.timeout_sec)
    return sorted(out.glob('frame_*.jpg'))


def _run(argv: list[str], timeout_sec: int) -> str:
    """Run one bounded ffmpeg-family call; loud on any refusal."""
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed binary, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ProbeError(f'probe_failed: {argv[0]} missing') from exc
    if proc.returncode != 0:
        raise ProbeError(f'probe_failed: {proc.stderr[-300:]}')
    return proc.stdout
