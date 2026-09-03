# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The state database: one file, and a view per service onto it.

Two properties this module exists for are tested as requirements, not
assumed: per-peer state must stop being rewritten whole on every event, and
one file must hold everything without services reading each other's rows.
The rest is the import, which has to survive every shape that ever shipped.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from minions.userbot.core.state import DB_NAME
from minions.userbot.core.state import Actor
from minions.userbot.core.state import Database
from minions.userbot.core.state import PeerRow
from minions.userbot.core.state import StateStore
from minions.userbot.core.state import adopt

if TYPE_CHECKING:
    from pathlib import Path

TOP_N = 2
"""How many rows the limited readout asks for."""

ONE_STANDING_ONE_LOG = 2
"""What one act on one subject costs: the counters, and the row behind them."""

CROWD = 1000
"""The audience size the old design produced a 47 323-line file at."""

MOOD = 0.25
"""A state value, carried through a reopen to prove it round-trips."""

TAKE_AT = 1000.0
"""A moment to bump at, so take_at is a value and not just "now"."""


def _store(tmp_path: Path, service: str = 'reactions') -> StateStore:
    """Return one service's view of the state database in a temp dir."""
    return Database(tmp_path / DB_NAME).store(service)


# ------------------------------------------------------------- peer rows


def test_unknown_peer_reads_as_an_empty_row(tmp_path: Path) -> None:
    """A peer we have never seen answers with zeroes, not with None."""
    assert _store(tmp_path).peer(77) == PeerRow(77)


def test_bump_accumulates_per_column(tmp_path: Path) -> None:
    """Counters add up; a column left out of the call is left alone."""
    store = _store(tmp_path)
    store.bump(77, {'offered': 1})
    store.bump(77, {'offered': 1, 'taken': 1})
    row = store.peer(77)
    assert (row.offered, row.taken, row.recip) == (2, 1, 0)


def test_a_take_stamps_take_at_and_an_offer_does_not(tmp_path: Path) -> None:
    """``take_at`` is the moment the next gap is measured from.

    It advances on exactly the bumps that count a take, which is why the
    caller no longer passes it: an offer landing at the same instant would
    otherwise make the following engagement's gap zero.
    """
    store = _store(tmp_path)
    store.bump(77, {'taken': 1}, TAKE_AT)
    store.bump(77, {'offered': 1}, TAKE_AT + 60)

    row = store.peer(77)
    assert row.take_at == TAKE_AT
    assert row.last_at == TAKE_AT + 60  # recency moved, the take did not


def test_peers_come_back_most_recent_first(tmp_path: Path) -> None:
    """Recency ordering is a column now, not dict insertion order."""
    store = _store(tmp_path)
    for peer in (1, 2, 3):
        store.bump(peer, {'offered': 1})
    store.bump(1, {'offered': 1})  # peer 1 is freshest again
    assert next(p.peer_id for p in store.peers()) == 1
    assert len(store.peers(limit=TOP_N)) == TOP_N


def test_a_thinner_answer_never_erases_a_fuller_one(tmp_path: Path) -> None:
    """Telegram shares what it feels like; a later blank must not erase.

    A resolution can come back with a username one time and only a first
    name the next. Overwriting field by field would make the second answer
    forget what the first taught us.
    """
    db = Database(tmp_path / DB_NAME)
    db.note_actor(Actor(77, 'user', username='real', first_name='Real'))
    db.note_actor(Actor(77, 'user', first_name='Realer'))

    got = db.actor(77)
    assert (got.username, got.first_name) == ('real', 'Realer')


def test_forget_drops_the_row(tmp_path: Path) -> None:
    """A peer rolled off the tracked set leaves no counters behind."""
    store = _store(tmp_path)
    store.bump(77, {'taken': 3})
    store.forget(77)
    assert store.peer(77) == PeerRow(77)


# --------------------------------------------------- one file, many services


def test_two_services_share_a_file_and_nothing_else(tmp_path: Path) -> None:
    """The same person seen by two services is two independent rows.

    This is what the single file has to buy back. A view is bound to its
    name at construction and no method below takes one, so a service cannot
    read or clear another's rows even by accident -- which is exactly what
    the old shared file, where every call passed the name, could not say.
    """
    db = Database(tmp_path / DB_NAME)
    likes, views = db.store('reactions'), db.store('stories')

    likes.bump(77, {'taken': TOP_N})
    likes.mark('shared-key')
    likes.write({'mood': MOOD})
    views.bump(77, {'taken': 1})
    views.mark('shared-key')
    views.write({'total_views': 1})

    assert likes.peer(77).taken == TOP_N
    assert views.peer(77).taken == 1
    assert likes.read() == {'mood': MOOD}
    assert views.read() == {'total_views': 1}

    views.keep_marks(())  # clears the STORY marks
    assert views.marked('shared-key') is False
    assert likes.marked('shared-key') is True


def test_a_view_carries_its_name_so_callers_never_pass_one(
    tmp_path: Path,
) -> None:
    """Not one store method takes a service argument.

    The shared file failed the first time because sixteen methods took the
    name as their first argument, so every call site could pass the wrong
    one. The binding is the fix, and this asserts it stays a binding.
    """
    taking = [
        name
        for name in dir(StateStore)
        if not name.startswith('_')
        and callable(getattr(StateStore, name, None))
        and 'service' in getattr(StateStore, name).__code__.co_varnames
    ]
    assert taking == []


def test_the_audience_lives_in_the_same_file(tmp_path: Path) -> None:
    """The users tables are there for anyone who opens the database.

    They carry no service column because they have no service: one
    channel's members belong to the profile, which is the whole reason
    users.db stopped being a file of its own.
    """
    db = Database(tmp_path / DB_NAME)
    tables = {
        str(r['name'])
        for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {'actors', 'audience', 'membership_events', 'messages'} <= tables
    assert 'service' not in {
        str(r['name']) for r in db.conn.execute('PRAGMA table_info(actors)')
    }


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


# -------------------------------------------------------- the state block


def test_the_state_block_round_trips_through_the_file(tmp_path: Path) -> None:
    """A reopened store reads back what the previous one wrote."""
    store = _store(tmp_path)
    store.write({'mood': MOOD, 'mood_day': '2026-08-28'})
    assert _store(tmp_path).read() == {'mood': MOOD, 'mood_day': '2026-08-28'}


def test_read_is_a_copy_not_the_live_block(tmp_path: Path) -> None:
    """Mutating what read() returned must not change stored state."""
    store = _store(tmp_path)
    store.write({'total_views': 1})
    store.read()['total_views'] = 999
    assert store.read()['total_views'] == 1


def test_an_unreadable_block_starts_fresh_but_read_strict_raises(
    tmp_path: Path,
) -> None:
    """Two readers, on purpose: one degrades, one refuses to.

    An engine reading "no mood yet" starts from its defaults and loses
    nothing. The poster reading "nothing was ever posted" disarms its
    re-post guard and republishes the backlog, so it must fail instead.
    """
    store = _store(tmp_path)
    store.conn.execute(
        "INSERT INTO state (service, blob) VALUES ('reactions', '{oops')"
    )
    store.conn.commit()

    assert store.read() == {}
    try:
        store.read_strict()
    except ValueError:
        return
    msg = 'read_strict swallowed a broken block'
    raise AssertionError(msg)


def test_the_block_survives_a_kill_with_nothing_flushed(
    tmp_path: Path,
) -> None:
    """Written through, not coalesced -- the watchdog never flushes.

    The JSON twin this replaces was rebuilt on demand, because rewriting a
    whole file per event was the cost this module exists to remove. A
    one-row commit has no such cost, so there is no window in which a
    ``write`` that returned is still only in memory: the store below is
    never closed, exactly as ``os._exit`` leaves it.
    """
    _store(tmp_path).write({'mood': MOOD})

    alone = sqlite3.connect(f'file:{tmp_path / DB_NAME}?mode=ro', uri=True)
    got = alone.execute('SELECT blob FROM state').fetchone()

    assert json.loads(got[0]) == {'mood': MOOD}


# ----------------------------------------------------------------- scale


def test_a_thousand_peers_still_write_one_row_each(tmp_path: Path) -> None:
    """The requirement this module exists for.

    The old design put per-peer maps in the same file as the cursors, so at
    a thousand peers the readable file was 652 KB / 47 323 lines and was
    rewritten on every comment. Peers are rows now: a crowd costs a crowd of
    rows and nothing else, and the block beside them is untouched by any of
    it.
    """
    store = _store(tmp_path)
    store.write({'mood': MOOD, 'alive': {'12': 5.0}})
    for i in range(CROWD):
        peer = -1000000000 - i
        store.bump(peer, {'offered': 3, 'taken': 2})
        store.mark(f'-100:7:{peer}')

    assert len(store.peers()) == CROWD
    assert store.read()['mood'] == MOOD


def test_a_write_costs_the_same_whatever_the_audience(tmp_path: Path) -> None:
    """A write must be O(1) in the AUDIENCE, not O(N).

    Proved through SQLite's own change counter: the same bump costs the same
    among one peer and among a hundred. What it costs is a standing row and
    a contact row per SUBJECT -- flat in how many people the account has ever
    met, and flat in which rung the act reached.
    """
    alone, crowded = _store(tmp_path), _store(tmp_path, 'stories')
    for i in range(100):
        crowded.bump(i, {'offered': 1})

    costs = []
    for store in (alone, crowded):
        before = store.conn.total_changes
        store.bump(50, {'offered': 1, 'taken': 1})
        costs.append(store.conn.total_changes - before)

    assert costs[0] == costs[1]
    assert costs[0] == ONE_STANDING_ONE_LOG


def test_the_database_file_stands_alone(tmp_path: Path) -> None:
    """A copy of the .db carries the rows, with no sibling files needed.

    Under WAL it did not: recent writes sat in a -wal file until a
    checkpoint, and the watchdog ends this process with os._exit, so no
    checkpoint ever ran. Copying the .db then produced a valid database
    with nothing in it -- and nothing said the data was in the file next
    door. Asserted by copying ONLY the .db, exactly as a backup does.
    """
    store = _store(tmp_path, 'stories')
    store.bump(77, {'offered': TOP_N, 'taken': 1})
    store.mark('77:1')
    # deliberately NOT closed: the crash path is what the WAL trap needed

    copy = tmp_path / 'copied.db'
    copy.write_bytes((tmp_path / DB_NAME).read_bytes())
    alone = sqlite3.connect(f'file:{copy}?mode=ro', uri=True)

    assert alone.execute('SELECT count(*) FROM standing').fetchone()[0] == 1
    assert alone.execute('SELECT count(*) FROM marks').fetchone()[0] == 1
    assert sorted(p.name for p in tmp_path.glob(f'{DB_NAME}-*')) == []


# ------------------------------------------------- adopting the older shapes

_SHARED_SCHEMA = """
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
"""Shape 1: the shared peers.db, at the OLDEST schema on record.

Deliberately the oldest: a file that predates take_at and the gap statistics
is what an install in the field actually has, and it is what the import has
to survive.
"""

_LEDGER_SCHEMA = """
CREATE TABLE peers (
    peer_id TEXT PRIMARY KEY, label TEXT NOT NULL DEFAULT '',
    offered INTEGER NOT NULL DEFAULT 0, taken INTEGER NOT NULL DEFAULT 0,
    recip INTEGER NOT NULL DEFAULT 0, last_at REAL NOT NULL DEFAULT 0,
    take_at REAL NOT NULL DEFAULT 0, gap_n INTEGER NOT NULL DEFAULT 0,
    gap_sum REAL NOT NULL DEFAULT 0, gap_sq REAL NOT NULL DEFAULT 0,
    burst INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE marks (key TEXT PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE cursor (id INTEGER PRIMARY KEY, blob TEXT NOT NULL);
"""
"""Shape 2a: a per-service ledger file, as reactions.db and stories.db were."""

_BLOCK_SCHEMA = """
CREATE TABLE state (id INTEGER PRIMARY KEY, blob TEXT NOT NULL);
"""
"""Shape 2b: a per-service block file, as aggregator/greeter/comod.db were."""

_USERS_SCHEMA = """
CREATE TABLE users (
    user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
    last_name TEXT, phone TEXT, first_seen REAL, last_seen REAL,
    msg_count INTEGER NOT NULL DEFAULT 0,
    subscribed INTEGER NOT NULL DEFAULT 0, updated_at REAL
);
CREATE TABLE membership_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    event TEXT NOT NULL, ts REAL NOT NULL, admin_log_id INTEGER UNIQUE
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL, msg_id INTEGER NOT NULL, root INTEGER,
    text TEXT, ts REAL NOT NULL, UNIQUE (chat_id, msg_id)
);
"""
"""Shape 3: users.db, which was never a service to begin with."""

LEGACY_OFFERED = 10
LEGACY_TAKEN = 7
LEGACY_RECIP = 2
LEGACY_LAST_AT = 5.0
LEGACY_USER = 4242
OLD_PEER = 5105052156


def _write(
    path: Path, schema: str, *rows: tuple[str, tuple[object, ...]]
) -> None:
    """Build one legacy database at a given schema, with rows in it."""
    conn = sqlite3.connect(str(path))
    conn.executescript(schema)
    for sql, args in rows:
        conn.execute(sql, args)
    conn.commit()
    conn.close()


def _shared(where: Path) -> None:
    """Write shape 1: peers.db + cursors.json, both keyed by engine."""
    _write(
        where / 'peers.db',
        _SHARED_SCHEMA,
        (
            'INSERT INTO peers (engine, peer_id, offered, taken, recip, '
            'last_at) VALUES (?, ?, ?, ?, ?, ?)',
            (
                'reactions',
                str(OLD_PEER),
                LEGACY_OFFERED,
                LEGACY_TAKEN,
                LEGACY_RECIP,
                LEGACY_LAST_AT,
            ),
        ),
        (
            'INSERT INTO marks (engine, key, at) VALUES (?, ?, ?)',
            ('stories', '77:1', 1.0),
        ),
    )
    (where / 'cursors.json').write_text(
        json.dumps({'reactions': {'mood': MOOD}, 'stories': {'views': 3}}),
        encoding='utf-8',
    )


def _per_service(where: Path) -> None:
    """Write shape 2: the six files one-database-per-service left behind."""
    _write(
        where / 'stories.db',
        _LEDGER_SCHEMA,
        (
            'INSERT INTO peers (peer_id, label, offered, taken) '
            'VALUES (?, ?, ?, ?)',
            ('360724480', '@eliza (360724480)', LEGACY_OFFERED, LEGACY_TAKEN),
        ),
        ('INSERT INTO marks (key, at) VALUES (?, ?)', ('360724480:397', 1.0)),
        (
            'INSERT INTO cursor (id, blob) VALUES (1, ?)',
            (json.dumps({'total_views': 38}),),
        ),
    )
    for service, block in (
        ('aggregator', {'posted': [{'title': 'Posted one'}]}),
        ('greeter', {'last_event_id': 41}),
        ('comod', {'nick': {'at': 1.0, 'amount': '50'}}),
    ):
        _write(
            where / f'{service}.db',
            _BLOCK_SCHEMA,
            (
                'INSERT INTO state (id, blob) VALUES (1, ?)',
                (json.dumps(block),),
            ),
        )


def _audience(where: Path) -> None:
    """Write shape 3: users.db with a member and a message."""
    _write(
        where / 'users.db',
        _USERS_SCHEMA,
        (
            'INSERT INTO users (user_id, username, msg_count) '
            'VALUES (?, ?, ?)',
            (LEGACY_USER, 'alice', 1),
        ),
        (
            'INSERT INTO messages (user_id, chat_id, msg_id, text, ts) '
            'VALUES (?, ?, ?, ?, ?)',
            (LEGACY_USER, -100, 7, 'hi', 1.0),
        ),
    )


def test_adopt_folds_every_older_shape_into_the_one_file(
    tmp_path: Path,
) -> None:
    """All three shapes at once, which is what a real directory holds.

    The shared pair predates the timing columns, so this also asserts the
    tolerance the in-place ALTER used to provide: a column the old file
    lacks starts at its default instead of failing the import.
    """
    _shared(tmp_path)
    _per_service(tmp_path)
    _audience(tmp_path)

    adopt(tmp_path)

    db = Database(tmp_path / DB_NAME)
    likes = db.store('reactions')
    row = likes.peer(OLD_PEER)  # shape 1, by its engine column
    assert (row.offered, row.taken, row.recip) == (
        LEGACY_OFFERED,
        LEGACY_TAKEN,
        LEGACY_RECIP,
    )
    assert (row.gap_n, row.gap_sum, row.burst) == (0, 0.0, 0)
    assert likes.read() == {'mood': MOOD}  # shape 1, from cursors.json

    views = db.store('stories')  # shape 2a, by its filename
    assert db.actor(360724480).username == 'eliza'
    assert views.marked('360724480:397') is True
    assert views.marked('77:1') is True  # and shape 1's mark, same service
    assert views.read() == {'total_views': 38}

    assert db.store('greeter').read() == {'last_event_id': 41}  # shape 2b
    assert db.store('aggregator').read_strict()['posted'] == [
        {'title': 'Posted one'}
    ]

    seen = db.conn.execute(  # shape 3, under no service at all
        'SELECT a.username, n.msg_count FROM actors a '
        'JOIN audience n ON n.peer_id = a.peer_id WHERE a.peer_id = ?',
        (LEGACY_USER,),
    ).fetchone()
    assert (seen['username'], seen['msg_count']) == ('alice', 1)
    assert db.conn.execute('SELECT count(*) FROM messages').fetchone()[0] == 1


def test_adopt_does_not_cross_the_services(tmp_path: Path) -> None:
    """A row lands under its own name and nobody else's."""
    _shared(tmp_path)
    _per_service(tmp_path)
    adopt(tmp_path)

    db = Database(tmp_path / DB_NAME)
    assert db.store('stories').peer(OLD_PEER) == PeerRow(OLD_PEER)
    assert db.store('reactions').marked('77:1') is False
    assert db.store('reactions').read() == {'mood': MOOD}


def test_adopt_runs_once_and_the_old_files_are_gone(tmp_path: Path) -> None:
    """Every imported file is renamed aside; the directory is one .db."""
    _shared(tmp_path)
    _per_service(tmp_path)
    _audience(tmp_path)

    adopt(tmp_path)

    assert sorted(p.name for p in tmp_path.glob('*.db')) == [DB_NAME]
    assert not (tmp_path / 'cursors.json').exists()


def test_a_resumed_import_never_undoes_live_state(tmp_path: Path) -> None:
    """An import interrupted before the rename must not replay over newer work.

    The planting and the renaming are not one transaction, so a kill between
    them leaves the old files in place with the one database already
    populated and in use. The next start finds both and imports again -- and
    by then the old file is stale by everything the bot has done since.
    """
    _shared(tmp_path)
    adopt(tmp_path)
    (tmp_path / 'peers.db.bak').rename(tmp_path / 'peers.db')  # killed here
    (tmp_path / 'cursors.json.bak').rename(tmp_path / 'cursors.json')
    store = _store(tmp_path)
    store.bump(OLD_PEER, {'taken': 1}, TAKE_AT)
    store.write({'mood': 0.9})

    adopt(tmp_path)  # a second start

    reopened = _store(tmp_path)
    assert reopened.peer(OLD_PEER).taken == LEGACY_TAKEN + 1
    assert reopened.read() == {'mood': 0.9}


def test_an_unreadable_file_keeps_its_name(tmp_path: Path) -> None:
    """What we could not import, we must not rename.

    A .bak nobody thinks to look for, with every ledger silently back at
    zero, is a worse outcome than a start that imports nothing and says so.
    The readable file beside it is still imported: one bad file is not a
    reason to strand the rest.
    """
    (tmp_path / 'broken.db').write_bytes(b'this is not a database')
    _per_service(tmp_path)

    adopt(tmp_path)

    assert (tmp_path / 'broken.db').exists()
    assert not (tmp_path / 'broken.db.bak').exists()
    assert Database(tmp_path / DB_NAME).store('greeter').read() == {
        'last_event_id': 41
    }


_KILLED_WAL_WRITER = """
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('PRAGMA journal_mode=WAL')
conn.executescript(sys.argv[2])
conn.execute("INSERT INTO peers (peer_id, taken) VALUES ('770', 1)")
conn.commit()
os._exit(0)
"""
"""A writer that dies the way the watchdog kills this bot.

``os._exit`` skips every close, so SQLite never checkpoints: the committed
row stays in ``stories.db-wal`` and both siblings are left on disk. Nothing
short of an abrupt exit produces that state -- a closed connection cleans up
after itself -- and it is the exact state the field install was found in.
"""


def test_adopt_leaves_no_orphan_journal_siblings(tmp_path: Path) -> None:
    """A WAL install is folded back into its .db before it is renamed.

    This is the mess being cleaned up, so the cleanup must not recreate it:
    renaming the file while -wal and -shm sat beside it would strand two
    files whose names match nothing left in the directory -- and strand them
    holding the only copy of the rows.
    """
    subprocess.run(  # noqa: S603 -- the interpreter running us, and a literal
        [
            sys.executable,
            '-c',
            _KILLED_WAL_WRITER,
            str(tmp_path / 'stories.db'),
            _LEDGER_SCHEMA,
        ],
        check=True,
    )
    assert sorted(p.name for p in tmp_path.glob('stories.db*')) == [
        'stories.db',
        'stories.db-shm',
        'stories.db-wal',
    ]

    adopt(tmp_path)

    assert sorted(p.name for p in tmp_path.glob('stories.db*')) == [
        'stories.db.bak'
    ]
    assert Database(tmp_path / DB_NAME).store('stories').peer(770).taken == 1


def test_adopt_on_a_fresh_directory_creates_only_the_database(
    tmp_path: Path,
) -> None:
    """No old files means no import -- and nothing invented but the file."""
    adopt(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [DB_NAME]


def test_a_write_cut_short_keeps_the_old_block_whole(tmp_path: Path) -> None:
    """A kill part-way through a write leaves the previous state (CT-A).

    The watchdog turns a hang into a hard ``os._exit(1)``, so an interrupted
    write is a case that happens. It used to be survived by writing a
    sibling temp file and renaming it; the store survives it by being one
    transaction, and an uncommitted transaction IS what a killed process
    leaves. Written here without a commit, exactly as that kill would.
    """
    _store(tmp_path).write({'n': 1})

    dying = sqlite3.connect(str(tmp_path / DB_NAME))
    dying.execute(
        "INSERT INTO state (service, blob) VALUES ('reactions', ?) "
        'ON CONFLICT (service) DO UPDATE SET blob = excluded.blob',
        (json.dumps({'n': 2}),),
    )
    dying.close()  # no commit: the process died holding the transaction

    assert _store(tmp_path).read() == {'n': 1}


def test_the_hand_rolled_json_files_are_adopted_too(tmp_path: Path) -> None:
    """An install that skipped a release still finds its state.

    The version before last imported these three into per-service databases
    and renamed them; an install that jumps straight from before that to
    here would find them untouched, and dropping the path would quietly
    start every one of them from zero.
    """
    (tmp_path / 'greeter_state.json').write_text(
        json.dumps({'last_event_id': 41}), encoding='utf-8'
    )
    (tmp_path / 'comod.json').write_text(
        json.dumps({'nick': {'at': 1.0, 'amount': '50'}}), encoding='utf-8'
    )

    adopt(tmp_path)

    db = Database(tmp_path / DB_NAME)
    assert db.store('greeter').read() == {'last_event_id': 41}
    assert db.store('comod').read()['nick'] == {'at': 1.0, 'amount': '50'}
    assert sorted(p.name for p in tmp_path.glob('*.json')) == []


def test_a_json_file_never_overwrites_a_database_that_has_the_row(
    tmp_path: Path,
) -> None:
    """The oldest copy on disk must not win over the newest.

    A directory can hold both: the ``.db`` the last release wrote and the
    ``.json`` it forgot to rename. Ordering the import by age would be a
    rule to get wrong, so the rule is simply that whatever is already in the
    one database stays.
    """
    (tmp_path / 'greeter_state.json').write_text(
        json.dumps({'last_event_id': 1}), encoding='utf-8'
    )
    _store(tmp_path, 'greeter').write({'last_event_id': 99})

    adopt(tmp_path)

    assert Database(tmp_path / DB_NAME).store('greeter').read() == {
        'last_event_id': 99
    }


def test_retiring_a_file_takes_its_stale_journal_with_it(
    tmp_path: Path,
) -> None:
    """A ``-journal`` left by a killed process must not outlive its database.

    It is inert by the time we rename -- the import opened the file, which
    is when SQLite rolls a journal back, and the rows are already copied --
    so leaving it would put one more file in the directory whose name
    matches nothing in it. That is the whole complaint being answered.
    """
    _per_service(tmp_path)
    (tmp_path / 'stories.db-journal').write_bytes(b'')

    adopt(tmp_path)

    assert sorted(p.name for p in tmp_path.glob('stories.db*')) == [
        'stories.db.bak'
    ]


def test_a_copy_of_the_database_beside_it_is_not_re_imported(
    tmp_path: Path,
) -> None:
    """A backup in the state directory must not be mistaken for older state.

    Copying userbot.db before poking at it is the first thing an operator
    does, and the copy lands right next to the original. It has a ``state``
    table like the old blob files, so only its shape says otherwise: a
    ``state`` that is keyed by service is THIS schema, and one arbitrary row
    out of it would land under the copy's filename as a service that never
    existed.
    """
    _store(tmp_path, 'greeter').write({'last_event_id': 41})
    backup = tmp_path / 'userbot-backup.db'
    backup.write_bytes((tmp_path / DB_NAME).read_bytes())

    adopt(tmp_path)

    db = Database(tmp_path / DB_NAME)
    named = {
        str(r['service']) for r in db.conn.execute('SELECT service FROM state')
    }
    assert named == {'greeter'}


def test_adopt_says_what_it_took(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The import is a one-shot on an unattended machine, so it reports.

    Without this the only way to learn whether a start imported six files or
    none is to open the database and count rows -- which is what the first
    person to meet an unexpected file actually ends up doing.
    """
    with caplog.at_level(logging.INFO, logger='userbot'):
        adopt(tmp_path)
    assert 'nothing older found' in caplog.text

    _per_service(tmp_path)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger='userbot'):
        adopt(tmp_path)
    assert 'stories.db' in caplog.text
    assert '4 adopted file(s)' in caplog.text


# --------------------------------------- folding THIS file's older shape

_OLD_SELF = """
CREATE TABLE peers (
    service TEXT NOT NULL, peer_id TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '', offered INTEGER NOT NULL DEFAULT 0,
    taken INTEGER NOT NULL DEFAULT 0, recip INTEGER NOT NULL DEFAULT 0,
    last_at REAL NOT NULL DEFAULT 0, take_at REAL NOT NULL DEFAULT 0,
    gap_n INTEGER NOT NULL DEFAULT 0, gap_sum REAL NOT NULL DEFAULT 0,
    gap_sq REAL NOT NULL DEFAULT 0, burst INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (service, peer_id)
);
CREATE TABLE users (
    user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
    last_name TEXT, phone TEXT, first_seen REAL, last_seen REAL,
    msg_count INTEGER NOT NULL DEFAULT 0,
    subscribed INTEGER NOT NULL DEFAULT 0, updated_at REAL
);
CREATE TABLE membership_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    event TEXT NOT NULL, ts REAL NOT NULL, admin_log_id INTEGER UNIQUE
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL, msg_id INTEGER NOT NULL, root INTEGER,
    text TEXT, ts REAL NOT NULL, UNIQUE (chat_id, msg_id)
);
"""
"""userbot.db as the version before this one wrote it.

Not an older FILE -- the same file, one schema back. This is what a deployed
install actually has on the morning of the upgrade.
"""

CHANNEL = -1001787638608


def _old_self(path: Path) -> None:
    """Write userbot.db at the previous schema, with a person and a channel."""
    _write(
        path,
        _OLD_SELF,
        (
            'INSERT INTO peers (service, peer_id, label, offered, taken) '
            'VALUES (?, ?, ?, ?, ?)',
            ('stories', '360724480', '@eliza (360724480)', 7, 5),
        ),
        (
            'INSERT INTO peers (service, peer_id, label) VALUES (?, ?, ?)',
            ('stories', str(CHANNEL), f'"Kanal" ({CHANNEL})'),
        ),
        (
            'INSERT INTO users (user_id, username, subscribed) '
            'VALUES (?, ?, ?)',
            (LEGACY_USER, 'alice', 1),
        ),
        (
            'INSERT INTO membership_events (user_id, event, ts) '
            'VALUES (?, ?, ?)',
            (LEGACY_USER, 'join', 1.0),
        ),
    )


def test_a_channel_folds_in_as_a_chat_not_a_person(tmp_path: Path) -> None:
    """The sign of a Telegram id says which it is, and the fold reads it.

    The story engine watches channels as well as people, so both are actors;
    folding a channel in as a 'user' would make ``kind`` a column that
    silently lies about half its rows.
    """
    _old_self(tmp_path / DB_NAME)

    db = Database(tmp_path / DB_NAME)

    assert db.actor(CHANNEL).kind == 'chat'
    assert db.actor(CHANNEL).title == 'Kanal'
    assert db.actor(360724480).kind == 'user'
    assert db.actor(360724480).username == 'eliza'


def test_an_older_file_opens_before_it_is_indexed(tmp_path: Path) -> None:
    """The fold runs BETWEEN the tables and the indexes, and must.

    An index names columns, so ``ix_messages_peer`` cannot be built until
    ``messages`` has a ``peer_id`` -- which the fold is what creates. Built
    with the tables, the very first open of a deployed file dies on a column
    the fold was about to make.
    """
    _old_self(tmp_path / DB_NAME)

    db = Database(tmp_path / DB_NAME)  # would raise if the order were wrong

    indexed = {
        str(r['name'])
        for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
    assert {'ix_messages_peer', 'ix_membership_peer'} <= indexed
    assert (
        db.conn.execute('SELECT peer_id FROM membership_events').fetchone()[
            'peer_id'
        ]
        == LEGACY_USER
    )


# --------------------------------------------- the relationship history

LIKED = 'like'
"""What the top rung is called on a story -- and the middle one on a comment.

The same word for two different rungs, which is the whole reason ``ACTS`` is
per service: a like costs nothing under a comment and is the strongest thing
we ever do to a story.
"""

STORY_A = 397
STORY_B = 398
WATCHED = 360724480


def test_the_history_answers_what_happened_with_one_person(
    tmp_path: Path,
) -> None:
    """Everything we ever did with a peer, in one query, newest first.

    This is the question the whole table exists for. It used to take four:
    the counters here, the dedup keys under a stringly-typed key in
    ``marks``, a rolling log inside a JSON blob, and the audience tables --
    none of which could be joined to the others, because the same person had
    two ids of two different types.
    """
    store = _store(tmp_path, 'stories')
    store.bump(WATCHED, {'offered': 1}, 100.0, (STORY_A,))
    store.bump(WATCHED, {'offered': 1, 'taken': 1}, 200.0, (STORY_B,))
    store.bump(WATCHED, {'recip': 1}, 300.0, (STORY_B,))
    store.bump(999, {'offered': 1}, 250.0, (1,))  # somebody else entirely

    got = store.history(WATCHED)

    # Newest first, in the words the person on the other side would use: one
    # story of theirs we let pass, one we opened, and then hearted. The
    # middle bump counts an offer AND a take and is ONE row -- a story seen,
    # not a story ignored beside the same story seen.
    assert [(c.act, c.subject) for c in got] == [
        (LIKED, STORY_B),
        ('seen', STORY_B),
        ('ignore', STORY_A),
    ]
    assert all(c.peer_id == WATCHED for c in got)


def test_the_counters_are_a_running_total_of_the_log(tmp_path: Path) -> None:
    """``standing`` must equal the log it summarises, for every peer.

    The invariant this tier exists to create. Before it, a surprising number
    had nothing behind it: asking why exposure read 44% meant running a
    simulation, because the counter was the only copy of its own input.
    Written in one transaction so the two cannot drift.
    """
    store = _store(tmp_path, 'stories')
    for i, peer in enumerate((11, 22, 33)):
        store.bump(peer, {'offered': i + 1}, 100.0 + i, tuple(range(i + 1)))
        store.bump(peer, {'offered': 1, 'taken': 1}, 200.0 + i, (9 + i,))
        if peer == 33:  # noqa: PLR2004 -- one of the peers written above
            store.bump(peer, {'recip': 1}, 300.0, (9 + i,))

    for row in store.peers():
        logged = store.tally(row.peer_id)
        assert row.offered == logged['offered']
        assert row.taken == logged['taken']
        assert row.recip == logged['recip']


def test_an_unnamed_act_is_still_one_row_in_the_log(tmp_path: Path) -> None:
    """A caller that cannot name what it acted on still logs the act.

    The COUNT is what the model reads off ``standing``, so the log has to
    carry the same number of rows whether or not anyone could say which
    story or comment each one was -- otherwise the invariant above holds
    only for the callers that happen to know.
    """
    store = _store(tmp_path)
    store.bump(77, {'offered': 3, 'taken': 3}, 100.0)  # three, none named

    assert store.acts(77)[LIKED] == 3  # noqa: PLR2004 -- the count above
    assert [c.subject for c in store.history(77)] == [0, 0, 0]


def test_the_history_is_a_service_view_like_everything_else(
    tmp_path: Path,
) -> None:
    """One person, two services, two histories -- the binding still holds."""
    db = Database(tmp_path / DB_NAME)
    db.store('stories').bump(77, {'taken': 1}, 100.0, (STORY_A,))
    db.store('reactions').bump(77, {'taken': 1}, 200.0, (5,))

    assert [c.subject for c in db.store('stories').history(77)] == [STORY_A]
    assert [c.subject for c in db.store('reactions').history(77)] == [5]


def test_a_chance_we_passed_on_is_logged_as_an_ignore(tmp_path: Path) -> None:
    """The pool of outcomes is ignore / seen / like, and the log says which.

    Every chance leaves exactly one row naming what we did with it, so the
    history reads as a list of DECISIONS. Logging the chance and the answer
    separately would put an ``ignore`` under every story we watched.
    """
    store = _store(tmp_path, 'stories')
    store.bump(WATCHED, {'offered': 1}, 100.0, (STORY_A,))
    store.bump(WATCHED, {'offered': 1, 'taken': 1}, 200.0, (STORY_B,))

    assert store.acts(WATCHED) == {'ignore': 1, 'seen': 1}


def test_the_same_rung_is_named_for_the_service_it_happened_in(
    tmp_path: Path,
) -> None:
    """A reciprocation is a heart on a story and a sticker on a comment.

    The identical bump, logged in two services, gets two words -- because it
    is two acts, and a shared vocabulary would report the rarest thing we do
    to a story under the same name as a throwaway reply to a comment.
    """
    db = Database(tmp_path / DB_NAME)
    for service in ('stories', 'reactions'):
        db.store(service).bump(WATCHED, {'recip': 1}, 100.0, (STORY_A,))

    assert db.store('stories').acts(WATCHED) == {LIKED: 1}
    assert db.store('reactions').acts(WATCHED) == {'sticker': 1}


def test_a_service_cannot_log_an_act_it_has_no_word_for(
    tmp_path: Path,
) -> None:
    """The CHECK is generated from ACTS, so the table refuses the rest.

    'seen' is a real act -- in the OTHER service. A row that files it under
    reactions is not a typo the log should keep quietly; nothing reads
    ``contact`` often enough to notice one on its own.
    """
    store = _store(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            'INSERT INTO contact (peer_id, at, service, act) '
            "VALUES (?, ?, 'reactions', 'seen')",
            (WATCHED, 100.0),
        )


def test_when_we_met_somebody_is_one_answer_for_the_whole_person(
    tmp_path: Path,
) -> None:
    """``met`` crosses services on purpose, and it is the only thing that.

    An arc belongs to a PERSON: one account is one person, so the day we met
    somebody is the same day whether the story engine noticed them first or
    the like engine did. Two clocks would put the same person in the
    honeymoon on one and the cold shoulder on the other.
    """
    db = Database(tmp_path / DB_NAME)
    db.store('reactions').bump(WATCHED, {'offered': 1}, 100.0, (5,))
    db.store('stories').bump(WATCHED, {'offered': 1}, 300.0, (STORY_A,))

    assert db.store('stories').met(WATCHED) == 100.0  # noqa: PLR2004
    assert db.store('reactions').met(WATCHED) == 100.0  # noqa: PLR2004
    assert db.store('stories').met(999) == 0.0  # never met: not a date
