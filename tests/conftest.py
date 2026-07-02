"""Shared fixtures: hermetic Settings over tmp_path (BLUEPRINT 13)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from minion_core.settings import Settings
from minion_core.settings import load

if TYPE_CHECKING:
    from pathlib import Path


def make_env(drive: Path, **extra: str) -> dict[str, str]:
    """A minimal env mapping rooted at a temp drive."""
    return {'DRIVE': str(drive), **extra}


def make_cfg(drive: Path, **extra: str) -> Settings:
    """Hermetic Settings plus the media tree of BLUEPRINT 1.2."""
    cfg = load(make_env(drive, **extra))
    for path in (
        cfg.inbox,
        cfg.pictures,
        cfg.state,
        cfg.regen,
        cfg.logs,
        cfg.print_queue,
        cfg.print_done,
        cfg.scripts,
    ):
        path.mkdir(parents=True, exist_ok=True)
    return cfg


@pytest.fixture
def cfg(tmp_path: Path) -> Settings:
    """Default hermetic Settings."""
    return make_cfg(tmp_path / 'drive')
