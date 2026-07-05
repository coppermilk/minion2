"""backend selection tests: toggle round-trip + which impl is live."""

from __future__ import annotations

import pytest

from minion_core.adapters.backend import GEMINI
from minion_core.adapters.backend import LOCAL
from minion_core.adapters.backend import BackendToggle
from minion_core.adapters.backend import select_backend
from tests.conftest import make_cfg
from tests.conftest import make_env


def test_toggle_defaults_to_local(tmp_path):
    """A fresh deploy is offline-first with no setup."""
    cfg = make_cfg(tmp_path / 'drive')
    assert BackendToggle(cfg).read() == LOCAL


def test_toggle_round_trip(tmp_path):
    """A written choice survives a fresh instance (STATE on disk)."""
    cfg = make_cfg(tmp_path / 'drive')
    BackendToggle(cfg).write(GEMINI)
    assert BackendToggle(cfg).read() == GEMINI
    BackendToggle(cfg).write(LOCAL)
    assert BackendToggle(cfg).read() == LOCAL


def test_toggle_rejects_unknown(tmp_path):
    """Only the two known values may be stored."""
    cfg = make_cfg(tmp_path / 'drive')
    with pytest.raises(ValueError, match='bogus'):
        BackendToggle(cfg).write('bogus')


def test_corrupt_toggle_reads_default(tmp_path):
    """A garbled toggle reads as the default, never crashes a pass."""
    cfg = make_cfg(tmp_path / 'drive')
    (cfg.state / 'model.backend').write_text('garbage', encoding='ascii')
    assert BackendToggle(cfg).read() == LOCAL


def test_select_backend_follows_toggle(tmp_path):
    """The live backend swaps with the toggle, no restart."""
    cfg = make_cfg(tmp_path / 'drive')
    env = make_env(tmp_path / 'drive')
    assert select_backend(cfg, env).name == 'local'
    BackendToggle(cfg).write(GEMINI)
    assert select_backend(cfg, env).name == 'gemini'


def test_env_default_backend_honoured(tmp_path):
    """MODEL_BACKEND sets the default before any switch is written."""
    cfg = make_cfg(tmp_path / 'drive', MODEL_BACKEND='gemini')
    assert BackendToggle(cfg).read() == GEMINI
    assert select_backend(cfg, make_env(tmp_path / 'drive')).name == 'gemini'
