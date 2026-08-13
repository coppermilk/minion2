# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""props bot: report composition + have/need matching."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Never

import numpy as np

from minions.bots.props import main as props
from tests.conftest import make_cfg

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _boom(_query: object) -> Never:
    msg = 'embed_text should not be called on a name match'
    raise AssertionError(msg)


def test_report_have_and_need() -> None:
    """Check report have and need."""
    assert props._report(['PrWand'], ['Lantern']) == (
        'Have: PrWand\nNeed: Lantern'
    )
    assert props._report([], []) == 'no props found in the scenario'


def test_split_name_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """A required prop whose Pr-name exists is had without embedding."""
    owned = {'PrWand': np.array([1.0, 0.0], dtype=np.float32)}
    monkeypatch.setattr(props, 'embed_text', _boom)
    have, need = props._split(['Wand'], owned)
    assert have == ['PrWand']
    assert need == []


def test_split_semantic_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """A near-synonym is matched by CLIP text->image similarity."""
    owned = {'PrWand': np.array([1.0, 0.0], dtype=np.float32)}
    monkeypatch.setattr(
        props, 'embed_text', lambda _q: np.array([1.0, 0.0], dtype=np.float32)
    )
    have, need = props._split(['Staff'], owned)  # name miss, semantic hit
    assert have == ['PrWand']
    assert need == []


def test_split_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """An orthogonal prop is reported as still-needed."""
    owned = {'PrWand': np.array([1.0, 0.0], dtype=np.float32)}
    monkeypatch.setattr(
        props, 'embed_text', lambda _q: np.array([0.0, 1.0], dtype=np.float32)
    )
    have, need = props._split(['Umbrella'], owned)
    assert have == []
    assert need == ['Umbrella']


def test_respond_uses_pasted_scenario(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A long message is the scenario; the reply lists have/need."""
    cfg = make_cfg(tmp_path / 'drive')
    env = {'DRIVE': str(tmp_path / 'drive')}
    monkeypatch.setattr(props, 'select_backend', lambda _c, _e: object())
    monkeypatch.setattr(props, 'list_props', lambda _s, _b: ['Wand'])
    monkeypatch.setattr(props, '_owned_props', lambda _c: {})
    out = props.respond(cfg, env, 'x' * 50)
    assert 'Need: Wand' in out
