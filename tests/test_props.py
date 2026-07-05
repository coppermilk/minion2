"""props bot: report composition + have/need matching."""

from __future__ import annotations

import numpy as np

from minions.props import main as props
from tests.conftest import make_cfg


def _boom(_query):
    raise AssertionError('embed_text should not be called on a name match')


def test_report_have_and_need():
    assert props._report(['PrWand'], ['Lantern']) == (
        'Have: PrWand\nNeed: Lantern'
    )
    assert props._report([], []) == 'no props found in the scenario'


def test_split_name_match(monkeypatch):
    """A required prop whose Pr-name exists is had without embedding."""
    owned = {'PrWand': np.array([1.0, 0.0], dtype=np.float32)}
    monkeypatch.setattr(props, 'embed_text', _boom)
    have, need = props._split(['Wand'], owned)
    assert have == ['PrWand']
    assert need == []


def test_split_semantic_match(monkeypatch):
    """A near-synonym is matched by CLIP text->image similarity."""
    owned = {'PrWand': np.array([1.0, 0.0], dtype=np.float32)}
    monkeypatch.setattr(
        props, 'embed_text', lambda _q: np.array([1.0, 0.0], dtype=np.float32)
    )
    have, need = props._split(['Staff'], owned)  # name miss, semantic hit
    assert have == ['PrWand']
    assert need == []


def test_split_missing(monkeypatch):
    """An orthogonal prop is reported as still-needed."""
    owned = {'PrWand': np.array([1.0, 0.0], dtype=np.float32)}
    monkeypatch.setattr(
        props, 'embed_text', lambda _q: np.array([0.0, 1.0], dtype=np.float32)
    )
    have, need = props._split(['Umbrella'], owned)
    assert have == []
    assert need == ['Umbrella']


def test_respond_uses_pasted_scenario(monkeypatch, tmp_path):
    """A long message is the scenario; the reply lists have/need."""
    cfg = make_cfg(tmp_path / 'drive')
    env = {'DRIVE': str(tmp_path / 'drive')}
    monkeypatch.setattr(props, 'select_backend', lambda _c, _e: object())
    monkeypatch.setattr(props, 'list_props', lambda _s, _b: ['Wand'])
    monkeypatch.setattr(props, '_owned_props', lambda _c: {})
    out = props.respond(cfg, env, 'x' * 50)
    assert 'Need: Wand' in out
