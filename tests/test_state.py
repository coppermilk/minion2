# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The state store: one interface, SQLite for truth, JSON for reading.

The point of this module is that per-peer state stops being rewritten whole
on every event, so the scale properties are tested here as requirements, not
assumed: a thousand peers must leave the readable file small, and one peer's
write must touch one row.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

import pytest

from minions.userbot.core.state import PeerRow
from minions.userbot.core.state import StateStore

if TYPE_CHECKING:
    from pathlib import Path

CURSOR_BUDGET_KB = 10
"""The readable twin must stay eyeball-sized at any audience size."""

REACTION_TAKES = 5
"""An arbitrary count, only there to differ from the stories engine's."""

TOP_N = 2
"""How many rows the limited readout asks for."""

CROWD = 1000
"""The audience size the old design produced a 47 323-line file at."""

MOOD = 0.25
"""A cursor value, carried through the twin to prove it round-trips."""


def _store(tmp_path: Path) -> StateStore:
    """Return a store over a temp database and cursor file."""
    return StateStore(tmp_path / 'peers.db', tmp_path / 'cursors.json')


# ------------------------------------------------------------- peer rows


def test_unknown_peer_reads_as_an_empty_row(tmp_path: Path) -> None:
    """A peer we have never seen answers with zeroes, not with None."""
    assert _store(tmp_path).peer('reactions', '77') == PeerRow('77')


def test_bump_accumulates_per_column(tmp_path: Path) -> None:
    """Counters add up; a column left out of the call is left alone."""
    store = _store(tmp_path)
    store.bump('reactions', '77', {'offered': 1})
    store.bump('reactions', '77', {'offered': 1, 'taken': 1})
    row = store.peer('reactions', '77')
    assert (row.offered, row.taken, row.recip) == (2, 1, 0)


def test_engines_do_not_share_a_peers_counters(tmp_path: Path) -> None:
    """The same person seen by two engines is two independent rows.

    The old design kept these in two files under different key names; one
    table with an engine column must not merge them by accident.
    """
    store = _store(tmp_path)
    store.bump('reactions', '77', {'taken': REACTION_TAKES})
    store.bump('stories', '77', {'taken': 1})
    assert store.peer('reactions', '77').taken == REACTION_TAKES
    assert store.peer('stories', '77').taken == 1


def test_peers_come_back_most_recent_first(tmp_path: Path) -> None:
    """Recency ordering is a column now, not dict insertion order."""
    store = _store(tmp_path)
    for peer in ('a', 'b', 'c'):
        store.bump('reactions', peer, {'offered': 1})
    store.bump('reactions', 'a', {'offered': 1})  # 'a' is freshest again
    assert next(p.peer_id for p in store.peers('reactions')) == 'a'
    assert len(store.peers('reactions', limit=TOP_N)) == TOP_N


def test_remember_ignores_a_blank_or_id_shaped_label(tmp_path: Path) -> None:
    """A failed resolution must not overwrite a real name already learned."""
    store = _store(tmp_path)
    store.remember('stories', '77', '@real')
    store.remember('stories', '77', '')
    store.remember('stories', '77', '77')
    assert store.peer('stories', '77').label == '@real'


def test_forget_drops_the_row(tmp_path: Path) -> None:
    """A peer rolled off the tracked set leaves no counters behind."""
    store = _store(tmp_path)
    store.bump('stories', '77', {'taken': 3})
    store.forget('stories', '77')
    assert store.peer('stories', '77') == PeerRow('77')


# ----------------------------------------------------------- dedup marks


def test_mark_is_true_once_and_false_after(tmp_path: Path) -> None:
    """The dedup answer: True means new, False means already handled."""
    store = _store(tmp_path)
    assert store.mark('reactions', '-100:7:42') is True
    assert store.mark('reactions', '-100:7:42') is False
    assert store.marked('reactions', '-100:7:42') is True
    assert store.marked('reactions', 'never-seen') is False


def test_keep_marks_prunes_what_no_prefix_covers(tmp_path: Path) -> None:
    """Keys for posts that rolled out of the watch window are dropped."""
    store = _store(tmp_path)
    store.mark('reactions', '-100:7:alice')
    store.mark('reactions', '-100:8:bob')
    store.keep_marks('reactions', ('-100:8:',))
    assert store.marked('reactions', '-100:8:bob') is True
    assert store.marked('reactions', '-100:7:alice') is False


def test_keep_marks_with_no_prefixes_clears_the_engine(tmp_path: Path) -> None:
    """No live posts means no live keys."""
    store = _store(tmp_path)
    store.mark('reactions', '-100:7:alice')
    store.mark('stories', 'kept')
    store.keep_marks('reactions', ())
    assert store.marked('reactions', '-100:7:alice') is False
    assert store.marked('stories', 'kept') is True  # other engine untouched


# --------------------------------------------------------------- cursors


def test_cursors_round_trip_through_the_file(tmp_path: Path) -> None:
    """A reopened store reads back what the previous one wrote."""
    store = _store(tmp_path)
    store.put_cursor('reactions', {'mood': MOOD, 'mood_day': '2026-08-28'})
    store.close()
    assert _store(tmp_path).cursor('reactions') == {
        'mood': MOOD,
        'mood_day': '2026-08-28',
    }


def test_cursor_is_a_copy_not_the_live_block(tmp_path: Path) -> None:
    """Mutating what cursor() returned must not change stored state."""
    store = _store(tmp_path)
    store.put_cursor('stories', {'total_views': 1})
    store.cursor('stories')['total_views'] = 999
    assert store.cursor('stories')['total_views'] == 1


def test_unreadable_cursor_file_starts_fresh(tmp_path: Path) -> None:
    """A corrupt twin degrades to empty; it is never the source of truth."""
    (tmp_path / 'cursors.json').write_text('{oops', encoding='utf-8')
    assert _store(tmp_path).cursor('reactions') == {}


def test_snapshot_is_not_written_per_write(tmp_path: Path) -> None:
    """The twin is rebuilt on demand -- that is what removes the O(N) cost."""
    store = _store(tmp_path)
    store.put_cursor('reactions', {'mood': 0.5})
    assert not (tmp_path / 'cursors.json').exists()
    store.snapshot()
    assert (tmp_path / 'cursors.json').exists()


# ----------------------------------------------------------------- scale


def test_a_thousand_peers_leave_the_readable_file_small(
    tmp_path: Path,
) -> None:
    """The requirement this module exists for.

    The old design put per-peer maps in the same file as the cursors, so at
    a thousand peers the readable file was 652 KB / 47 323 lines and was
    rewritten on every comment. Peers now live in the database; the twin
    holds only bounded cursors and must stay eyeball-sized.
    """
    store = _store(tmp_path)
    for i in range(CROWD):
        peer = str(-1000000000 - i)
        store.bump('reactions', peer, {'offered': 3, 'taken': 2})
        store.remember('reactions', peer, f'@user{i}')
        store.mark('reactions', f'-100:7:{peer}')
    store.put_cursor('reactions', {'mood': MOOD, 'alive': {'12': 5.0}})
    store.snapshot()

    twin = (tmp_path / 'cursors.json').read_text(encoding='utf-8')
    assert len(twin) < CURSOR_BUDGET_KB * 1024
    assert len(store.peers('reactions')) == CROWD
    assert json.loads(twin)['reactions']['mood'] == MOOD


def test_one_peer_write_touches_one_row(tmp_path: Path) -> None:
    """A write must be O(1) in the audience, not O(N).

    Proved through SQLite's own change counter: bumping one peer among a
    hundred reports exactly one changed row.
    """
    store = _store(tmp_path)
    for i in range(100):
        store.bump('stories', str(i), {'offered': 1})
    before = store._conn.total_changes
    store.bump('stories', '50', {'taken': 1})
    assert store._conn.total_changes - before == 1


@pytest.mark.parametrize('engine', ['reactions', 'stories'])
def test_store_survives_reopen(tmp_path: Path, engine: str) -> None:
    """Rows and marks persist across a restart, cursors included."""
    store = _store(tmp_path)
    store.bump(engine, '77', {'offered': TOP_N, 'taken': 1})
    store.mark(engine, 'k')
    store.put_cursor(engine, {'n': 1})
    store.close()

    reopened = _store(tmp_path)
    assert reopened.peer(engine, '77').offered == TOP_N
    assert reopened.marked(engine, 'k') is True
    assert reopened.cursor(engine) == {'n': 1}


# ------------------------------------------------------------- migration

_LEGACY_SCHEMA = """
CREATE TABLE peers (
    engine  TEXT    NOT NULL,
    peer_id TEXT    NOT NULL,
    label   TEXT    NOT NULL DEFAULT '',
    offered INTEGER NOT NULL DEFAULT 0,
    taken   INTEGER NOT NULL DEFAULT 0,
    recip   INTEGER NOT NULL DEFAULT 0,
    last_at REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (engine, peer_id)
);
"""
"""The peers table as it shipped, before the timing columns existed."""

LEGACY_OFFERED = 10
LEGACY_TAKEN = 7
LEGACY_RECIP = 2


def _legacy_db(path: Path) -> None:
    """Write a database at the pre-timing schema, with one peer in it."""
    conn = sqlite3.connect(str(path))
    conn.executescript(_LEGACY_SCHEMA)
    conn.execute(
        'INSERT INTO peers (engine, peer_id, offered, taken, recip, last_at) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        ('reactions', 'old', LEGACY_OFFERED, LEGACY_TAKEN, LEGACY_RECIP, 5.0),
    )
    conn.commit()
    conn.close()


def test_a_deployed_database_gains_the_timing_columns(tmp_path: Path) -> None:
    """An existing peers.db is migrated in place, keeping every count.

    CREATE TABLE IF NOT EXISTS does nothing to a table that is already there,
    so without the ALTER the first bump naming gap_n would fail on every
    deployed install.
    """
    _legacy_db(tmp_path / 'peers.db')
    row = _store(tmp_path).peer('reactions', 'old')
    assert (row.offered, row.taken, row.recip) == (
        LEGACY_OFFERED,
        LEGACY_TAKEN,
        LEGACY_RECIP,
    )
    assert row.last_at == 5.0  # noqa: PLR2004 -- the value written above
    assert (row.gap_n, row.gap_sum, row.gap_sq, row.burst) == (0, 0.0, 0.0, 0)


def test_migrating_twice_changes_nothing(tmp_path: Path) -> None:
    """Reopening a migrated database is a no-op, not a duplicate column."""
    _legacy_db(tmp_path / 'peers.db')
    first = _store(tmp_path)
    first.bump('reactions', 'old', {'gap_n': 1, 'gap_sum': 60.0})
    first.close()

    row = _store(tmp_path).peer('reactions', 'old')
    assert (row.gap_n, row.gap_sum) == (1, 60.0)
