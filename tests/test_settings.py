"""Settings tests: REQ-CFG-001 and the precedence contract."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from minion_core.settings import BadConfig
from minion_core.settings import load

if TYPE_CHECKING:
    from pathlib import Path


def test_relative_drive_raises() -> None:
    """REQ-CFG-001: a relative path override is rejected at load."""
    with pytest.raises(BadConfig, match='bad_config'):
        load({'DRIVE': 'relative/drive'})


def test_relative_source_dir_raises(tmp_path: Path) -> None:
    """REQ-CFG-001 covers every path field, not just DRIVE."""
    with pytest.raises(BadConfig, match='SOURCE_DIRS'):
        load({'DRIVE': str(tmp_path), 'SOURCE_DIRS': 'Downloads'})


def test_missing_drive_raises() -> None:
    """No DRIVE, no start: the tree has exactly one root."""
    with pytest.raises(BadConfig, match='DRIVE'):
        load({})


def test_defaults_and_coercion(tmp_path: Path) -> None:
    """Every field coerces from one env line."""
    cfg = load(
        {
            'DRIVE': str(tmp_path),
            'DOWNLOAD_TIMEOUT_SEC': '5',
            'YTDLP_PLAYER_CLIENTS': 'web,android',
        }
    )
    assert cfg.download_timeout_sec == 5
    assert cfg.ytdlp_player_clients == ('web', 'android')
    assert cfg.quota_bytes > 0
    assert cfg.source_dirs == ()


def test_derived_paths_hang_off_drive(tmp_path: Path) -> None:
    """The tree of BLUEPRINT 1.2 derives from the one root."""
    cfg = load({'DRIVE': str(tmp_path)})
    assert cfg.inbox == tmp_path / '_inbox'
    assert cfg.pictures == tmp_path / 'pictures'
    assert cfg.state == tmp_path / 'bots' / '_data' / 'state'
    assert cfg.regen == tmp_path / 'bots' / '_data' / 'regen'
    assert cfg.logs == tmp_path / 'bots' / '_data' / 'logs'
    assert cfg.print_done == tmp_path / 'print' / '_done'
    assert cfg.bot_done('fetch') == tmp_path / 'bots' / 'fetch' / 'done'


def test_precedence_is_the_mapping_you_pass(tmp_path: Path) -> None:
    """No import-order rituals; the passed mapping wins."""
    a = load({'DRIVE': str(tmp_path / 'a')})
    b = load({'DRIVE': str(tmp_path / 'b')})
    assert a.drive != b.drive
