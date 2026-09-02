# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The state store: one service, one database, one file to look at.

Two properties this module exists for are tested as requirements, not
assumed: per-peer state must stop being rewritten whole on every event, and
the database file must be the whole story -- no engine column deciding whose
row is whose, no sibling journal files, no JSON twin to fall out of step.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from typing import TYPE_CHECKING

from minions.userbot.core.state import PeerRow
from minions.userbot.core.state import StateStore
from minions.userbot.core.state import adopt

if TYPE_CHECKING:
    from pathlib import Path

TOP_N = 2
"""How many rows the limited readout asks for."""

CROWD = 1000
"""The audience size the old design produced a 47 323-line file at."""

MOOD = 0.25
"""A cursor value, carried through a reopen to prove it round-trips."""

TAKE_AT = 1000.0
"""A moment to bump at, so take_at is a value and not just "now"."""


def _store(tmp_path: Path, service: str = 'reactions') -> StateStore:
    """Return a store over one service's database in a temp dir."""
    return StateStore(tmp_path / f'{service}.db')


# ------------------------------------------------------------- peer rows


def test_unknown_peer_reads_as_an_empty_row(tmp_path: Path) -> None:
    """A peer we have never seen answers with zeroes, not with None."""
    assert _store(tmp_path).peer('77') == PeerRow('77')


def test_bump_accumulates_per_column(tmp_path: Path) -> None:
    """Counters add up; a column left out of the call is left alone."""
    store = _store(tmp_path)
    store.bump('77', {'offered': 1})
    store.bump('77', {'offered': 1, 'taken': 1})
    row = store.peer('77')
    assert (row.offered, row.taken, row.recip) == (2, 1, 0)


def test_a_take_stamps_take_at_and_an_offer_does_not(tmp_path: Path) -> None:
    """``take_at`` is the moment the next gap is measured from.

    It advances on exactly the bumps that count a take, which is why the
    caller no longer passes it: an offer landing at the same instant would
    otherwise make the following engagement's gap zero.
    """
    store = _store(tmp_path)
    store.bump('77', {'taken': 1}, TAKE_AT)
    store.bump('77', {'offered': 1}, TAKE_AT + 60)

    row = store.peer('77')
    assert row.take_at == TAKE_AT
    assert row.last_at == TAKE_AT + 60  # recency moved, the take did not


def test_services_do_not_share_a_peers_counters(tmp_path: Path) -> None:
    """The same person seen by two services is two independent rows.

    They used to be told apart by an engine column inside one shared file;
    now they are told apart by being in different files, which is the same
    guarantee with nothing to get wrong.
    """
    store = _store(tmp_path, 'reactions')
    other = _store(tmp_path, 'stories')
    store.bump('77', {'taken': TOP_N})
    other.bump('77', {'taken': 1})
    assert store.peer('77').taken == TOP_N
    assert other.peer('77').taken == 1


def test_peers_come_back_most_recent_first(tmp_path: Path) -> None:
    """Recency ordering is a column now, not dict insertion order."""
    store = _store(tmp_path)
    for peer in ('a', 'b', 'c'):
        store.bump(peer, {'offered': 1})
    store.bump('a', {'offered': 1})  # 'a' is freshest again
    assert next(p.peer_id for p in store.peers()) == 'a'
    assert len(store.peers(limit=TOP_N)) == TOP_N


def test_remember_ignores_a_blank_or_id_shaped_label(tmp_path: Path) -> None:
    """A failed resolution must not overwrite a real name already learned."""
    store = _store(tmp_path)
    store.remember('77', '@real')
    store.remember('77', '')
    store.remember('77', '77')
    assert store.peer('77').label == '@real'


def test_forget_drops_the_row(tmp_path: Path) -> None:
    """A peer rolled off the tracked set leaves no counters behind."""
    store = _store(tmp_path)
    store.bump('77', {'taken': 3})
    store.forget('77')
    assert store.peer('77') == PeerRow('77')


# ----------------------------------------------------------- dedup marks


def test_mark_is_true_once_and_false_after(tmp_path: Path) -> None:
    """The dedup answer: True means new, False means already handled."""
    store = _store(tmp_path)
    assert store.mark('-100:7:42') is True
    assert store.mark('-100:7:42') is False
    assert store.marked('-100:7:42') is True
    assert store.marked('never-seen') is False


def test_keep_marks_prunes_what_no_prefix_covers(tmp_path: Path) -> None:
    """Keys for posts that rolled out of the watch window are dropped."""
    store = _store(tmp_path)
    store.mark('-100:7:alice')
    store.mark('-100:8:bob')
    store.keep_marks(('-100:8:',))
    assert store.marked('-100:8:bob') is True
    assert store.marked('-100:7:alice') is False


def test_keep_marks_with_no_prefixes_clears_the_service(
    tmp_path: Path,
) -> None:
    """No live posts means no live keys -- and only for this service."""
    store = _store(tmp_path, 'reactions')
    other = _store(tmp_path, 'stories')
    store.mark('-100:7:alice')
    other.mark('kept')
    store.keep_marks(())
    assert store.marked('-100:7:alice') is False
    assert other.marked('kept') is True  # another service is another file


# --------------------------------------------------------------- cursors


def test_cursors_round_trip_through_the_database(tmp_path: Path) -> None:
    """A reopened store reads back what the previous one wrote."""
    store = _store(tmp_path)
    store.put_cursor({'mood': MOOD, 'mood_day': '2026-08-28'})
    store.close()
    assert _store(tmp_path).cursor() == {
        'mood': MOOD,
        'mood_day': '2026-08-28',
    }


def test_cursor_is_a_copy_not_the_live_block(tmp_path: Path) -> None:
    """Mutating what cursor() returned must not change stored state."""
    store = _store(tmp_path)
    store.put_cursor({'total_views': 1})
    store.cursor()['total_views'] = 999
    assert store.cursor()['total_views'] == 1


def test_an_unreadable_cursor_block_starts_fresh(tmp_path: Path) -> None:
    """A corrupt block degrades to empty rather than taking the bot down."""
    store = _store(tmp_path)
    store._conn.execute("INSERT INTO cursor (id, blob) VALUES (1, '{oops')")
    store._conn.commit()
    assert store.cursor() == {}


def test_the_cursor_survives_a_kill_with_nothing_flushed(
    tmp_path: Path,
) -> None:
    """Written through, not coalesced -- the watchdog never flushes.

    The JSON twin this replaces was rebuilt on demand, because rewriting a
    whole file per event was the cost this module exists to remove. A
    one-row commit has no such cost, so there is no window in which a
    ``put_cursor`` that returned is still only in memory: the store below
    is never closed, exactly as ``os._exit`` leaves it.
    """
    _store(tmp_path).put_cursor({'mood': MOOD})

    reopened = sqlite3.connect(
        f'file:{tmp_path / "reactions.db"}?mode=ro', uri=True
    )
    got = reopened.execute('SELECT blob FROM cursor').fetchone()

    assert json.loads(got[0]) == {'mood': MOOD}


# ----------------------------------------------------------------- scale


def test_a_thousand_peers_still_write_one_row_each(tmp_path: Path) -> None:
    """The requirement this module exists for.

    The old design put per-peer maps in the same file as the cursors, so at
    a thousand peers the readable file was 652 KB / 47 323 lines and was
    rewritten on every comment. Peers are rows now: a crowd costs a crowd of
    rows and nothing else, and the cursor beside them is untouched by any of
    it.
    """
    store = _store(tmp_path)
    store.put_cursor({'mood': MOOD, 'alive': {'12': 5.0}})
    for i in range(CROWD):
        peer = str(-1000000000 - i)
        store.bump(peer, {'offered': 3, 'taken': 2})
        store.remember(peer, f'@user{i}')
        store.mark(f'-100:7:{peer}')

    assert len(store.peers()) == CROWD
    assert store.cursor()['mood'] == MOOD


def test_one_peer_write_touches_one_row(tmp_path: Path) -> None:
    """A write must be O(1) in the audience, not O(N).

    Proved through SQLite's own change counter: bumping one peer among a
    hundred reports exactly one changed row.
    """
    store = _store(tmp_path)
    for i in range(100):
        store.bump(str(i), {'offered': 1})
    before = store._conn.total_changes
    store.bump('50', {'taken': 1})
    assert store._conn.total_changes - before == 1


def test_store_survives_reopen(tmp_path: Path) -> None:
    """Rows and marks persist across a restart, the cursor included."""
    store = _store(tmp_path)
    store.bump('77', {'offered': TOP_N, 'taken': 1})
    store.mark('k')
    store.put_cursor({'n': 1})
    store.close()

    reopened = _store(tmp_path)
    assert reopened.peer('77').offered == TOP_N
    assert reopened.marked('k') is True
    assert reopened.cursor() == {'n': 1}


# ------------------------------------------------- the file is the database


def test_the_database_file_stands_alone(tmp_path: Path) -> None:
    """A copy of the .db carries the rows, with no sibling files needed.

    Under WAL it did not: recent writes sat in a -wal file until a
    checkpoint, and the watchdog ends this process with os._exit, so no
    checkpoint ever ran. Copying the .db then produced a valid database
    with nothing in it -- and nothing said the data was in the file next
    door. Asserted by copying ONLY the .db, exactly as a backup does.
    """
    store = _store(tmp_path, 'stories')
    store.bump('77', {'offered': TOP_N, 'taken': 1})
    store.mark('77:1')
    # deliberately NOT closed: the crash path is what the WAL trap needed

    copy = tmp_path / 'copied.db'
    copy.write_bytes((tmp_path / 'stories.db').read_bytes())
    alone = sqlite3.connect(f'file:{copy}?mode=ro', uri=True)

    assert alone.execute('SELECT count(*) FROM peers').fetchone()[0] == 1
    assert alone.execute('SELECT count(*) FROM marks').fetchone()[0] == 1


def test_no_sibling_journal_files_are_left_behind(tmp_path: Path) -> None:
    """The .db is the whole story -- no -wal, no -shm to copy alongside."""
    store = _store(tmp_path, 'stories')
    store.bump('77', {'offered': 1})

    siblings = sorted(p.name for p in tmp_path.glob('stories.db-*'))

    assert siblings == []


# --------------------------------------------------- adopting the old shape

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
CREATE TABLE marks (
    engine TEXT NOT NULL,
    key    TEXT NOT NULL,
    at     REAL NOT NULL,
    PRIMARY KEY (engine, key)
);
"""
"""The shared tables as they shipped, before the timing columns existed.

Deliberately the OLDEST shape on record: a file that predates take_at and
the gap statistics is the one an install in the field actually has, and it
is what the import has to survive.
"""

LEGACY_OFFERED = 10
LEGACY_TAKEN = 7
LEGACY_RECIP = 2
LEGACY_LAST_AT = 5.0


def _legacy(where: Path) -> None:
    """Write the shared peers.db and cursors.json an old install has."""
    conn = sqlite3.connect(str(where / 'peers.db'))
    conn.executescript(_LEGACY_SCHEMA)
    conn.execute(
        'INSERT INTO peers (engine, peer_id, offered, taken, recip, last_at) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (
            'reactions',
            'old',
            LEGACY_OFFERED,
            LEGACY_TAKEN,
            LEGACY_RECIP,
            LEGACY_LAST_AT,
        ),
    )
    conn.execute(
        'INSERT INTO marks (engine, key, at) VALUES (?, ?, ?)',
        ('stories', '77:1', 1.0),
    )
    conn.commit()
    conn.close()
    (where / 'cursors.json').write_text(
        json.dumps({'reactions': {'mood': MOOD}, 'stories': {'views': 3}}),
        encoding='utf-8',
    )


def test_adopt_splits_the_shared_files_by_service(tmp_path: Path) -> None:
    """Every count, mark and cursor lands in the file named for its owner.

    The shared pair predates the timing columns, so this also asserts the
    tolerance the in-place ALTER used to provide: what is missing starts at
    its default instead of failing the import.
    """
    _legacy(tmp_path)
    adopt(tmp_path)

    row = StateStore(tmp_path / 'reactions.db').peer('old')
    assert (row.offered, row.taken, row.recip) == (
        LEGACY_OFFERED,
        LEGACY_TAKEN,
        LEGACY_RECIP,
    )
    assert row.last_at == LEGACY_LAST_AT
    assert (row.gap_n, row.gap_sum, row.gap_sq, row.burst) == (0, 0.0, 0.0, 0)
    assert StateStore(tmp_path / 'reactions.db').cursor() == {'mood': MOOD}
    assert StateStore(tmp_path / 'stories.db').marked('77:1') is True
    assert StateStore(tmp_path / 'stories.db').cursor() == {'views': 3}


def test_adopt_does_not_cross_the_services(tmp_path: Path) -> None:
    """A row is carried by its engine column and by nothing else."""
    _legacy(tmp_path)
    adopt(tmp_path)

    assert StateStore(tmp_path / 'stories.db').peer('old') == PeerRow('old')
    assert StateStore(tmp_path / 'reactions.db').marked('77:1') is False


def test_adopt_runs_once_and_cannot_undo_newer_state(tmp_path: Path) -> None:
    """The old pair is set aside, so a restart cannot re-import it.

    Without this a stale shared file would overwrite every count made since
    the move on every start -- the failure mode that makes an import worse
    than no import at all.
    """
    _legacy(tmp_path)
    adopt(tmp_path)
    store = StateStore(tmp_path / 'reactions.db')
    store.bump('old', {'taken': 1}, TAKE_AT)
    store.put_cursor({'mood': 0.9})
    store.close()

    adopt(tmp_path)  # a second start

    reopened = StateStore(tmp_path / 'reactions.db')
    assert reopened.peer('old').taken == LEGACY_TAKEN + 1
    assert reopened.cursor() == {'mood': 0.9}
    assert not (tmp_path / 'peers.db').exists()
    assert not (tmp_path / 'cursors.json').exists()


_KILLED_WAL_WRITER = """
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('PRAGMA journal_mode=WAL')
conn.executescript(sys.argv[2])
conn.execute("INSERT INTO peers (engine, peer_id, taken)"
             " VALUES ('stories', 'w', 1)")
conn.commit()
os._exit(0)
"""
"""A writer that dies the way the watchdog kills this bot.

``os._exit`` skips every close, so SQLite never checkpoints: the committed
row stays in ``peers.db-wal`` and both siblings are left on disk. Nothing
short of an abrupt exit produces that state -- a closed connection cleans up
after itself -- and it is the exact state the field install was found in.
"""


def test_a_resumed_import_never_undoes_live_state(tmp_path: Path) -> None:
    """An import interrupted before the rename must not replay over newer work.

    The planting and the renaming are not one transaction, so a kill between
    them leaves the shared pair in place with the per-service files already
    populated and in use. The next start finds both and imports again -- and
    at that point the old file is stale by everything the bot has done since.
    """
    _legacy(tmp_path)
    adopt(tmp_path)
    (tmp_path / 'peers.db.bak').rename(tmp_path / 'peers.db')  # killed here
    (tmp_path / 'cursors.json.bak').rename(tmp_path / 'cursors.json')
    store = StateStore(tmp_path / 'reactions.db')
    store.bump('old', {'taken': 1}, TAKE_AT)
    store.put_cursor({'mood': 0.9})
    store.close()

    adopt(tmp_path)

    reopened = StateStore(tmp_path / 'reactions.db')
    assert reopened.peer('old').taken == LEGACY_TAKEN + 1
    assert reopened.cursor() == {'mood': 0.9}


def test_an_unreadable_shared_database_keeps_its_name(tmp_path: Path) -> None:
    """What we could not import, we must not rename.

    A .bak nobody thinks to look for, with every ledger silently back at
    zero, is a worse outcome than a start that imports nothing and says so.
    """
    (tmp_path / 'peers.db').write_bytes(b'this is not a database')
    (tmp_path / 'cursors.json').write_text('{"stories": {"n": 1}}')

    adopt(tmp_path)

    assert (tmp_path / 'peers.db').exists()
    assert (tmp_path / 'cursors.json').exists()
    assert sorted(p.name for p in tmp_path.glob('*.bak')) == []


def test_adopt_leaves_no_orphan_journal_siblings(tmp_path: Path) -> None:
    """A WAL install is folded back into its .db before it is renamed.

    This is the mess being cleaned up, so the cleanup must not recreate it:
    renaming peers.db while -wal and -shm sat beside it would strand two
    files in the state directory whose names match nothing left in it -- and
    strand them holding the only copy of the rows.
    """
    subprocess.run(  # noqa: S603 -- the interpreter running us, and a literal
        [
            sys.executable,
            '-c',
            _KILLED_WAL_WRITER,
            str(tmp_path / 'peers.db'),
            _LEGACY_SCHEMA,
        ],
        check=True,
    )
    assert sorted(p.name for p in tmp_path.glob('peers.db*')) == [
        'peers.db',
        'peers.db-shm',
        'peers.db-wal',
    ]

    adopt(tmp_path)

    assert sorted(p.name for p in tmp_path.glob('peers.db*')) == [
        'peers.db.bak'
    ]
    assert StateStore(tmp_path / 'stories.db').peer('w').taken == 1


def test_adopt_on_a_fresh_directory_creates_nothing(tmp_path: Path) -> None:
    """No old files means no import -- and no empty databases invented."""
    adopt(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == []
