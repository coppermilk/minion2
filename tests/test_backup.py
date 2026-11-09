# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Gentle-to-Drive state: live local + periodic Drive backup + restore.

Pins the moving parts independently of a running bot: the live/backup path
split keyed on LOCAL_STATE_DIR, the coalesced commits (a write stays in an
open transaction until a flush), the online backup copy, and the cold-start
restore that never clobbers newer local state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.conftest import install_telethon_stub

install_telethon_stub()  # before importing main (pulls the telethon adapter)

from minions.userbot import main  # noqa: E402
from minions.userbot.core import config  # noqa: E402
from minions.userbot.core.state import DB_NAME  # noqa: E402
from minions.userbot.core.state import Database  # noqa: E402

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_MOOD = 0.5


def _db(dirpath: Path) -> Database:
    """Open (creating the dir) the state database under ``dirpath``."""
    dirpath.mkdir(parents=True, exist_ok=True)
    return Database(dirpath / DB_NAME)


def _clear_env(mp: pytest.MonkeyPatch) -> None:
    """Drop every state-path env var so each case starts from a clean slate."""
    for name in (
        'DRIVE',
        'LOCAL_STATE_DIR',
        'AGGREGATOR_STATE_DIR',
        'TELEGRAM_SESSION_FILE',
    ):
        mp.delenv(name, raising=False)


# --- the path split -------------------------------------------------------


def test_without_local_dir_live_is_drive_and_no_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default (no LOCAL_STATE_DIR): live stays on DRIVE, nothing to mirror."""
    _clear_env(monkeypatch)
    drive = tmp_path / 'drive'
    monkeypatch.setenv('DRIVE', str(drive))
    aggr = drive / 'bots' / 'aggregator'
    assert config.live_base() == aggr
    assert config.backup_base() is None
    assert config.resolve_session_path() == aggr / 'telethon'
    assert config.resolve_session_backup() is None


def test_local_dir_redirects_live_and_names_drive_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With LOCAL_STATE_DIR: live goes local, DRIVE becomes the backup."""
    _clear_env(monkeypatch)
    drive = tmp_path / 'drive'
    local = tmp_path / 'local'
    monkeypatch.setenv('DRIVE', str(drive))
    monkeypatch.setenv('LOCAL_STATE_DIR', str(local))
    aggr = drive / 'bots' / 'aggregator'
    assert config.live_base() == local
    assert config.backup_base() == aggr
    assert config.resolve_state_dir(tmp_path / 'pkg') == local
    assert config.resolve_session_path() == local / 'telethon'
    assert config.resolve_session_backup() == aggr / 'telethon'


# --- coalesced commits + online backup ------------------------------------


def test_coalesced_write_stays_open_until_flush(tmp_path: Path) -> None:
    """A coalesced write holds its transaction open; flush commits it."""
    db = _db(tmp_path)
    db.set_coalesce(on=True)
    db.store('reactions').write({'mood': _MOOD})
    assert db.conn.in_transaction is True  # held, not yet committed
    db.flush()
    assert db.conn.in_transaction is False  # one real commit
    assert db.store('reactions').read().get('mood') == _MOOD
    db.close()


def test_backup_to_makes_a_readable_copy(tmp_path: Path) -> None:
    """backup_to writes a consistent snapshot that opens and reads back."""
    db = _db(tmp_path / 'live')
    db.set_coalesce(on=True)
    db.store('reactions').write({'mood': _MOOD})  # still uncommitted
    dst = tmp_path / 'drive' / DB_NAME
    db.backup_to(dst)  # flushes first, so the snapshot has the write
    assert Database(dst).store('reactions').read().get('mood') == _MOOD


def test_close_flushes_pending_writes(tmp_path: Path) -> None:
    """close() commits what coalescing held, so nothing is lost on exit."""
    db = _db(tmp_path / 'live')
    db.set_coalesce(on=True)
    db.store('reactions').write({'mood': _MOOD})
    db.close()
    reopened = Database(tmp_path / 'live' / DB_NAME)
    assert reopened.store('reactions').read().get('mood') == _MOOD


# --- cold-start restore ----------------------------------------------------


def _bare_bot(backup_base: Path) -> main.Userbot:
    """Build a Userbot shell with only the fields restore touches."""
    bot = object.__new__(main.Userbot)
    bot._backup_base = backup_base
    bot._dbs = {}
    return bot


def test_restore_profile_seeds_empty_live_from_backup(tmp_path: Path) -> None:
    """A cold local profile is restored from its Drive backup."""
    backup = tmp_path / 'drive'
    _db(backup).store('reactions').write({'mood': _MOOD})
    live = tmp_path / 'local'
    _bare_bot(backup)._restore_profile(live)
    assert (live / DB_NAME).exists()
    got = Database(live / DB_NAME).store('reactions').read().get('mood')
    assert got == _MOOD


def test_restore_profile_never_overwrites_present_live(
    tmp_path: Path,
) -> None:
    """A live file already on disk wins -- the backup must not clobber it."""
    backup = tmp_path / 'drive'
    _db(backup).store('reactions').write({'mood': 1.0})
    live = tmp_path / 'local'
    _db(live).store('reactions').write({'mood': _MOOD})
    _bare_bot(backup)._restore_profile(live)
    got = Database(live / DB_NAME).store('reactions').read().get('mood')
    assert got == _MOOD  # the newer local value, untouched
