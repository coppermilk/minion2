# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Shared fixtures: hermetic Settings over tmp_path (BLUEPRINT 13)."""

from __future__ import annotations

import sys
import types
from typing import TYPE_CHECKING

import pytest

from minion_core.settings import Settings
from minion_core.settings import load

if TYPE_CHECKING:
    from pathlib import Path


class _AnyMeta(type):
    """A class whose every attribute is another such class (nested access)."""

    def __getattr__(cls, name: str) -> type:
        return _AnyMeta(name, (), {})


def install_telethon_stub() -> None:
    """Register fake Telethon modules so ``aggregator.main`` imports w/o it.

    Telethon is a runtime-only extra (``tg``), absent from the test extras,
    yet the aggregator binds a handful of its names at import. Each stub
    answers any ``from telethon... import X`` with a throwaway class. Call it
    before importing ``minions.aggregator.main``; idempotent.
    """
    if 'telethon' in sys.modules:
        return
    for name in (
        'telethon',
        'telethon.events',
        'telethon.utils',
        'telethon.tl',
        'telethon.tl.functions',
        'telethon.tl.functions.messages',
        'telethon.tl.functions.stories',
        'telethon.tl.types',
    ):
        module = types.ModuleType(name)
        module.__getattr__ = lambda attr: _AnyMeta(attr, (), {})
        module.__path__ = []  # type: ignore[attr-defined]
        sys.modules[name] = module


def make_env(drive: Path, **extra: str) -> dict[str, str]:
    """Return a minimal env mapping rooted at a temp drive."""
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
