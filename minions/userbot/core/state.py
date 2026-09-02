# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""One service's state, in one file named after that service.

The state a service keeps is two shapes with opposite scaling, and that is
why it is stored the way it is:

* PER-PEER rows -- who commented, who was engaged, whose stories were seen.
  These grow with the audience. Modelled at 300 peers the hand-rolled JSON
  files came to 163 KB / 4 425 lines and 199 KB / 14 423 lines, and every
  one of the fifteen call sites that touched state rewrote its whole file: a
  heartbeat storing one number rewrote 163 KB, 1440 times a day. Here an
  event writes ONE ROW, whatever the audience size.
* CURSORS -- mood, the session marks, the daily counters. A few dozen
  scalars, bounded forever. One row holding one JSON blob, because there is
  nothing in a dozen scalars worth querying.

Both live in ONE DATABASE PER SERVICE. They used to be spread across a
``peers.db`` keyed by an engine column and a ``cursors.json`` beside it,
both shared by every service in the directory -- so a listing of the state
directory told you nothing about who owned what, and with the sibling files
SQLite's WAL leaves behind it told you something actively wrong. Now
``stories.db`` is the story engine's state, all of it, and ``adopt`` splits
an old install into that shape once.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from dataclasses import fields
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

log = logging.getLogger('userbot')

_SCHEMA = """
CREATE TABLE IF NOT EXISTS peers (
    peer_id TEXT    PRIMARY KEY,
    label   TEXT    NOT NULL DEFAULT '',
    offered INTEGER NOT NULL DEFAULT 0,
    taken   INTEGER NOT NULL DEFAULT 0,
    recip   INTEGER NOT NULL DEFAULT 0,
    last_at REAL    NOT NULL DEFAULT 0,
    take_at REAL    NOT NULL DEFAULT 0,
    gap_n   INTEGER NOT NULL DEFAULT 0,
    gap_sum REAL    NOT NULL DEFAULT 0,
    gap_sq  REAL    NOT NULL DEFAULT 0,
    burst   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS peers_recent ON peers (last_at DESC);
CREATE TABLE IF NOT EXISTS marks (
    key TEXT PRIMARY KEY,
    at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cursor (
    id   INTEGER PRIMARY KEY CHECK (id = 1),
    blob TEXT    NOT NULL
);
"""
"""The whole of a service's state: its peers, its dedup marks, its cursor.

The cursor table's CHECK is the schema saying out loud that there is exactly
one cursor block here -- which is true now that the file belongs to one
service, and was the thing the old engine-keyed JSON could not say.
"""


COUNTERS = (
    'offered',
    'taken',
    'recip',
    'gap_n',
    'gap_sum',
    'gap_sq',
    'burst',
)
"""Every per-peer column a bump adds to.

All of them are ADDITIVE, which is the whole reason the timing statistics fit
here: a running mean would need read-modify-write, but a sum does not, so the
one ``INSERT ... ON CONFLICT DO UPDATE`` below still writes a single row per
event whatever the audience size.
"""

JOURNAL = 'DELETE'
"""How SQLite journals a write -- deliberately NOT the WAL default.

WAL keeps recent writes in a sibling ``<service>.db-wal`` until a checkpoint,
so the ``.db`` alone is stale, sometimes by everything: the watchdog ends this
process with ``os._exit``, which never closes the connection, so no checkpoint
ever runs. A backup or a hand copy of the database then carries a valid file
with zero rows in it, which is exactly as alarming as it sounds and gives no
hint that the data is in the file next door.

WAL buys nothing here to pay for that. It exists for concurrent readers during
a write, and this is one process holding one connection per service --
/status reads through the same object that writes. In DELETE mode the journal
is transient, the two sibling files never appear, and the ``.db`` is complete
after every commit. Writes fsync one at a time, which at a few dozen events a
day is not a cost worth measuring.

Switching an existing WAL database is safe and automatic: SQLite checkpoints
it on the mode change and removes the sibling files.
"""

_BUMP_SQL = (  # noqa: S608 -- the column names are COUNTERS, never input
    'INSERT INTO peers (peer_id, {cols}, last_at, take_at) '
    'VALUES (?, {marks}, ?, ?) '
    'ON CONFLICT (peer_id) DO UPDATE SET {sets}, '
    'last_at = excluded.last_at, '
    'take_at = max(take_at, excluded.take_at)'
).format(
    cols=', '.join(COUNTERS),
    marks=', '.join('?' * len(COUNTERS)),
    sets=', '.join(f'{name} = {name} + excluded.{name}' for name in COUNTERS),
)
"""One upsert over every counter, built from the list above.

Three combine rules, and the difference between them is the point:
``COUNTERS`` add up, ``last_at`` is overwritten with the caller's moment, and
``take_at`` takes the later of the two -- so a bump that is not an engagement
of ours can pass 0 and leave it alone.
"""


@dataclass(frozen=True)
class PeerRow:
    """One peer's standing with this service.

    ``offered`` is how many chances they gave us (their comments, their
    stories), ``taken`` how many we engaged, ``recip`` how many we answered
    with the stronger act. ``last_at`` orders the /status readout by recency
    -- it used to depend on dict insertion order, which was silently fragile
    -- and is also where the next gap is measured from.

    The gap statistics are the raw material for the two attachment factors
    that are about TIMING rather than counts (see ``core/relationship.py``):
    the dispersion of ``gap_sum``/``gap_sq`` gives how irregular our attention
    is, and ``burst`` how much of it arrives all at once. Sums, not a history,
    so a peer costs the same four numbers forever.
    """

    peer_id: str
    label: str = ''
    offered: int = 0
    taken: int = 0
    recip: int = 0
    last_at: float = 0.0
    take_at: float = 0.0  # when WE last engaged; where the next gap starts
    gap_n: int = 0  # gaps between our touches observed so far
    gap_sum: float = 0.0  # their total, in seconds
    gap_sq: float = 0.0  # the total of their squares, for the dispersion
    burst: int = 0  # touches that joined an already-open session


def _connect(path: Path | str) -> sqlite3.Connection:
    """Open a service database, applying the schema and the journal mode."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.execute(f'PRAGMA journal_mode={JOURNAL}')
    conn.commit()
    return conn


@dataclass
class StateStore:
    """One service's state: its peers, its dedup marks, its cursor block."""

    db_path: Path | str

    def __post_init__(self) -> None:
        """Open the database and apply the schema."""
        self._conn = _connect(self.db_path)

    # --- per-peer rows ---------------------------------------------------

    def peer(self, peer_id: str) -> PeerRow:
        """Return one peer's row, or an empty row if it has none yet."""
        got = self._conn.execute(
            'SELECT * FROM peers WHERE peer_id = ?', (peer_id,)
        ).fetchone()
        return _row(got) if got is not None else PeerRow(peer_id)

    def peers(self, limit: int = 0) -> list[PeerRow]:
        """Return the peers, most recently interacted-with first."""
        sql = 'SELECT * FROM peers ORDER BY last_at DESC'
        args: tuple[object, ...] = ()
        if limit > 0:
            sql, args = sql + ' LIMIT ?', (limit,)
        return [_row(r) for r in self._conn.execute(sql, args)]

    def bump(
        self,
        peer_id: str,
        counts: Mapping[str, float],
        at: float | None = None,
    ) -> None:
        """Add to a peer's counters, creating the row on first sight.

        ``counts`` names the columns to add to (any of ``COUNTERS``); absent
        ones are left alone. The gap statistics are floats, the rest whole
        numbers, and SQLite stores each by its column's affinity. One row is
        written, whatever the audience size -- that is the whole point of
        this module.

        ``at`` is when the CALLER decided, and it is what ``last_at`` records.
        The engines run on an injectable clock, so a store reading
        ``time.time()`` here would measure every gap across two clocks -- the
        same in production, wrong under a fixed test clock, which is exactly
        the shape of bug that survives a test suite. Omitting it means now.

        ``take_at`` advances on exactly the bumps that count a take, because
        that is what it means: the moment the next gap is measured from.
        ``last_at`` cannot serve both -- it answers "when did anything happen
        with this peer", which orders the readout and the trim, and the offer
        lands at the very instant the engagement following it measures back
        from.
        """
        adds = [counts.get(name, 0) for name in COUNTERS]
        moment = time.time() if at is None else at
        took = moment if counts.get('taken') else 0.0
        self._conn.execute(_BUMP_SQL, (peer_id, *adds, moment, took))
        self._conn.commit()

    def remember(self, peer_id: str, label: str) -> None:
        """Cache a peer's display name; a blank or id-like label is ignored."""
        if not label or label == peer_id:
            return
        self._conn.execute(
            'INSERT INTO peers (peer_id, label) VALUES (?, ?) '
            'ON CONFLICT (peer_id) DO UPDATE SET label = excluded.label',
            (peer_id, label),
        )
        self._conn.commit()

    def forget(self, peer_id: str) -> None:
        """Drop a peer entirely (it rolled off the tracked set)."""
        self._conn.execute('DELETE FROM peers WHERE peer_id = ?', (peer_id,))
        self._conn.commit()

    def trim_peers(self, keep: int) -> list[str]:
        """Drop all but the ``keep`` most recent peers; return what went.

        The tracked set is bounded so a long-running account does not carry
        every peer it ever met. The caller clears whatever else it keyed by
        those peers -- the store does not know their mark format.
        """
        if keep <= 0:
            return []
        rows = self._conn.execute(
            'SELECT peer_id FROM peers '
            'ORDER BY last_at DESC LIMIT -1 OFFSET ?',
            (keep,),
        ).fetchall()
        dropped = [str(r['peer_id']) for r in rows]
        for peer_id in dropped:
            self.forget(peer_id)
        return dropped

    # --- dedup marks -----------------------------------------------------

    def mark(self, key: str) -> bool:
        """Record a dedup key; return whether it was NEW.

        This is the "have we already acted here" question -- a reacted
        (post, person) key, a seen story id. False means the caller has
        already handled it and must not act again.
        """
        cur = self._conn.execute(
            'INSERT OR IGNORE INTO marks (key, at) VALUES (?, ?)',
            (key, time.time()),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def marked(self, key: str) -> bool:
        """Whether a dedup key has already been recorded."""
        got = self._conn.execute(
            'SELECT 1 FROM marks WHERE key = ?', (key,)
        ).fetchone()
        return got is not None

    def count_marks(self) -> int:
        """How many dedup keys this service currently holds."""
        got = self._conn.execute('SELECT count(*) FROM marks').fetchone()
        return int(got[0])

    def drop_marks(self, prefix: str) -> None:
        """Drop every mark under one prefix (a peer that rolled off)."""
        self._conn.execute(
            'DELETE FROM marks WHERE key LIKE ?', (f'{prefix}%',)
        )
        self._conn.commit()

    def trim_marks(self, prefix: str, keep: int) -> None:
        """Keep only the newest ``keep`` marks under one prefix.

        A peer whose stories we have watched for years would otherwise
        accumulate a mark per story forever. 0 keeps them all.
        """
        if keep <= 0:
            return
        self._conn.execute(
            'DELETE FROM marks WHERE key LIKE ? AND key NOT IN '
            '(SELECT key FROM marks WHERE key LIKE ? '
            'ORDER BY at DESC LIMIT ?)',
            (f'{prefix}%', f'{prefix}%', keep),
        )
        self._conn.commit()

    def keep_marks(self, prefixes: tuple[str, ...]) -> None:
        """Drop the marks that no live prefix covers any more.

        Only the last few posts are ever matched, so a key for a post that
        rolled out of the window can never fire again; pruning keeps the
        table bounded. An empty prefix tuple clears the service.
        """
        if not prefixes:
            self._conn.execute('DELETE FROM marks')
        else:
            keep = ' OR '.join(['key LIKE ?'] * len(prefixes))
            self._conn.execute(
                f'DELETE FROM marks WHERE NOT ({keep})',  # noqa: S608 -- placeholders, not values
                tuple(f'{p}%' for p in prefixes),
            )
        self._conn.commit()

    # --- the cursor block ------------------------------------------------

    def cursor(self) -> dict[str, object]:
        """Return this service's cursor block (empty when it has none yet)."""
        got = self._conn.execute(
            'SELECT blob FROM cursor WHERE id = 1'
        ).fetchone()
        if got is None:
            return {}
        try:
            raw = json.loads(got[0])
        except ValueError:
            log.warning('state: %s has an unreadable cursor', self.db_path)
            return {}
        return dict(raw) if isinstance(raw, dict) else {}

    def put_cursor(self, cursor: Mapping[str, object]) -> None:
        """Replace this service's cursor block, right now.

        Written through rather than coalesced into a periodic rebuild: the
        block is a few dozen scalars, so this costs what a bump costs, and
        the watchdog's ``os._exit`` leaves nothing waiting to be flushed.
        The JSON file this replaces had to be rebuilt whole, which is why it
        could not be.
        """
        self._conn.execute(
            'INSERT INTO cursor (id, blob) VALUES (1, ?) '
            'ON CONFLICT (id) DO UPDATE SET blob = excluded.blob',
            (json.dumps(cursor, ensure_ascii=False),),
        )
        self._conn.commit()

    def close(self) -> None:
        """Release the database handle."""
        self._conn.close()


def _row(row: sqlite3.Row) -> PeerRow:
    """Build a PeerRow from one database row."""
    return PeerRow(
        peer_id=str(row['peer_id']),
        label=str(row['label']),
        offered=int(row['offered']),
        taken=int(row['taken']),
        recip=int(row['recip']),
        last_at=float(row['last_at']),
        take_at=float(row['take_at']),
        gap_n=int(row['gap_n']),
        gap_sum=float(row['gap_sum']),
        gap_sq=float(row['gap_sq']),
        burst=int(row['burst']),
    )


# ------------------------------------------- adopting the shared old shape

LEGACY_DB = 'peers.db'
LEGACY_CURSORS = 'cursors.json'
"""The two files every service in a directory used to share.

Both were keyed by an engine column/key, which is the shape being undone:
one file per service says who owns what, a listing says it too, and nothing
has to agree on a name to find its own rows.
"""

PEER_COLUMNS = tuple(f.name for f in fields(PeerRow))
MARK_COLUMNS = ('key', 'at')
"""What a row is worth carrying over, named once so the import cannot drift
from the schema above."""


def adopt(where: Path) -> None:
    """Split a shared state directory into one database per service, once.

    Every engine's rows, marks and cursor block move into ``<engine>.db``;
    the two shared files are then renamed aside, so this runs once and a
    restart cannot re-import a stale file over newer state. What a
    per-service database already holds wins -- the import adds, it never
    overwrites.

    Nothing is renamed unless the import got through. A shared database we
    could not read must keep its name, because the alternative is a ``.bak``
    nobody looks for and every ledger silently back at zero.
    """
    shared, cursors = where / LEGACY_DB, where / LEGACY_CURSORS
    engines = _legacy_engines(shared)
    if engines is None:  # unreadable: leave every old file exactly as it is
        log.error('state: %s cannot be read; not adopting it', shared)
        return
    blocks = _legacy_cursors(cursors)
    for engine in sorted(engines | set(blocks)):
        conn = _connect(where / f'{engine}.db')
        try:
            _inherit(conn, shared, engine)
            _seed_cursor(conn, blocks.get(engine, {}))
        finally:
            conn.close()
    _retire(shared)
    _retire(cursors)


def _legacy_engines(shared: Path) -> set[str] | None:
    """Return every engine the shared database holds state for.

    An empty set means there is nothing to import; ``None`` means the file
    is there but would not open, which is a different answer and has to stay
    one -- see ``adopt``.

    Opening and CLOSING it is also what folds a WAL install back together:
    SQLite checkpoints and removes the ``-wal`` and ``-shm`` siblings when
    the last connection closes cleanly, which the killed process that wrote
    them never did. So they are gone before the file is renamed aside,
    instead of outliving it under names that match nothing left in the
    directory -- while holding the only copy of the rows.
    """
    if not shared.exists():
        return set()
    conn = sqlite3.connect(str(shared))
    try:
        return {
            str(row[0])
            for table in ('peers', 'marks')
            for row in conn.execute(f'SELECT DISTINCT engine FROM {table}')  # noqa: S608 -- a literal, not input
        }
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _legacy_cursors(path: Path) -> dict[str, dict[str, object]]:
    """Read the shared cursors file: one block per engine, keyed by name."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        log.warning('state: %s unreadable; its cursors are lost', path.name)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}


def _inherit(conn: sqlite3.Connection, shared: Path, engine: str) -> None:
    """Copy one engine's rows and marks out of the shared database.

    Only the columns both schemas have are carried, so a shared file from
    before the timing columns existed simply leaves those at their defaults
    -- the same tolerance the in-place ALTER used to provide, now in the one
    place that still meets an old table.
    """
    if not shared.exists():
        return
    conn.execute('ATTACH DATABASE ? AS old', (str(shared),))
    try:
        wanted = (('peers', PEER_COLUMNS), ('marks', MARK_COLUMNS))
        for table, columns in wanted:
            names = ', '.join(_common(conn, table, columns))
            if not names:
                continue
            conn.execute(
                f'INSERT OR IGNORE INTO {table} ({names}) '  # noqa: S608 -- schema names, not input
                f'SELECT {names} FROM old.{table} WHERE engine = ?',
                (engine,),
            )
        conn.commit()
    finally:
        conn.execute('DETACH DATABASE old')


def _common(
    conn: sqlite3.Connection, table: str, wanted: tuple[str, ...]
) -> tuple[str, ...]:
    """Return the wanted columns the attached OLD table actually has."""
    have = {
        str(row['name'])
        for row in conn.execute(f'PRAGMA old.table_info({table})')
    }
    return tuple(name for name in wanted if name in have)


def _seed_cursor(
    conn: sqlite3.Connection, block: Mapping[str, object]
) -> None:
    """Plant an inherited cursor block, unless the service already has one."""
    if not block:
        return
    conn.execute(
        'INSERT OR IGNORE INTO cursor (id, blob) VALUES (1, ?)',
        (json.dumps(block, ensure_ascii=False),),
    )
    conn.commit()


def _retire(path: Path) -> None:
    """Rename a legacy file aside, so the import can only run once."""
    if path.exists():
        path.rename(path.with_suffix(path.suffix + '.bak'))
