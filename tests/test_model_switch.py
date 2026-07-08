"""model-switch bot: the command handler flips the toggle."""

from __future__ import annotations

from minion_core.adapters.backend import BackendToggle
from minions.model_switch.main import reply_for
from tests.conftest import make_cfg


def test_switch_flips_and_reports(tmp_path):
    """local/gemini set the toggle; status reads it back."""
    cfg = make_cfg(tmp_path / 'drive')
    toggle = BackendToggle(cfg)
    assert 'gemini' in reply_for(toggle, 'status')  # default
    assert reply_for(toggle, ' LOCAL ') == 'backend set to local'
    assert toggle.read() == 'local'
    assert reply_for(toggle, 'gemini') == 'backend set to gemini'
    assert toggle.read() == 'gemini'


def test_switch_unknown_gives_help(tmp_path):
    """A stray word lists the accepted commands, changes nothing."""
    cfg = make_cfg(tmp_path / 'drive')
    reply = reply_for(BackendToggle(cfg), 'wut')
    assert 'local' in reply
    assert 'gemini' in reply
    assert BackendToggle(cfg).read() == 'gemini'  # unchanged (default)
