"""Runtime admin config: defaults, overrides and unknown-key safety."""

from __future__ import annotations

from typing import TYPE_CHECKING

from minion_core.adapters.admin import SETTINGS
from minion_core.adapters.admin import admin_config

if TYPE_CHECKING:
    from pathlib import Path


def test_defaults_until_a_value_is_set(tmp_path: Path) -> None:
    """An unset key reads its registered default; unknown keys are ''."""
    cfg = admin_config(tmp_path)
    assert cfg.get('bed_broadcast_sec') == '0'
    assert cfg.get('wishlist_enabled') == '1'
    assert cfg.get('nope') == ''  # not in the registry


def test_set_get_reset_roundtrip_and_persist(tmp_path: Path) -> None:
    """A set value persists to disk and reset restores the default."""
    cfg = admin_config(tmp_path)
    assert cfg.set('bed_broadcast_sec', '3600')
    assert cfg.get('bed_broadcast_sec') == '3600'
    assert admin_config(tmp_path).get('bed_broadcast_sec') == '3600'
    assert cfg.reset('bed_broadcast_sec')
    assert cfg.get('bed_broadcast_sec') == '0'  # back to the default


def test_unknown_key_is_rejected_without_writing(tmp_path: Path) -> None:
    """set/reset on an unknown key return False and store nothing."""
    cfg = admin_config(tmp_path)
    assert not cfg.set('nope', 'x')
    assert not cfg.reset('nope')
    assert set(cfg.all()) == {s.key for s in SETTINGS}  # only known keys


def test_all_lists_every_registered_setting(tmp_path: Path) -> None:
    """all() covers exactly the registry, defaults included."""
    values = admin_config(tmp_path).all()
    assert set(values) == {s.key for s in SETTINGS}
    assert values['wishlist_enabled'] == '1'


def test_effective_prefers_override_then_the_fallback(tmp_path: Path) -> None:
    """effective() returns an override if set, else the env/default seed."""
    cfg = admin_config(tmp_path)
    assert cfg.effective('donation_chat', 'env-chat') == 'env-chat'
    assert cfg.set('donation_chat', 'admin-chat')
    assert cfg.effective('donation_chat', 'env-chat') == 'admin-chat'
