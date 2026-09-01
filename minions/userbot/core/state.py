# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The one place engine state is written, and the one place it is read.

Two kinds of state used to share one JSON file per engine, and that pairing
is what stopped scaling:

* PER-PEER rows -- who commented, who was engaged, whose stories were seen.
  These grow with the audience. Modelled at 300 peers the two state files
  came to 163 KB / 4 425 lines and 199 KB / 14 423 lines, and every one of
  the fifteen call sites that touched state rewrote its whole file. A
  heartbeat storing one number rewrote 163 KB, 1440 times a day.
* CURSORS -- mood, the session marks, the daily counters. A few dozen
  scalars, bounded forever, and the only part an operator ever wants to read.

So they are stored differently. Peer rows and dedup marks go to SQLite, one
row written per event (the pattern ``engines/users.py`` already uses here).
Cursors go to ``cursors.json``, which stays a couple of screens long at any
audience size.

``StateStore`` is the single interface over both: callers never choose a
backend. SQLite is the source of truth on read; the JSON twin is rebuilt
FROM it, so the two cannot drift apart permanently. The rebuild is
coalesced rather than run per write -- writing the readable file on every
event would put the O(N) rewrite straight back. Set ``SNAPSHOT_EVERY_WRITE``
to make it synchronous instead.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

log = logging.getLogger('userbot')

SNAPSHOT_EVERY_WRITE = False
"""Rebuild the JSON twin on every write instead of on demand.

Off by default: the twin exists to be read by a human, and rebuilding it per
event restores exactly the O(N) rewrite this module was written to remove.
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS peers (
    engine  TEXT    NOT NULL,
    peer_id TEXT    NOT NULL,
    label   TEXT    NOT NULL DEFAULT '',
    offered INTEGER NOT NULL DEFAULT 0,
    taken   INTEGER NOT NULL DEFAULT 0,
    recip   INTEGER NOT NULL DEFAULT 0,
    last_at REAL    NOT NULL DEFAULT 0,
    take_at REAL    NOT NULL DEFAULT 0,
    gap_n   INTEGER NOT NULL DEFAULT 0,
    gap_sum REAL    NOT NULL DEFAULT 0,
    gap_sq  REAL    NOT NULL DEFAULT 0,
    burst   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (engine, peer_id)
);
CREATE INDEX IF NOT EXISTS peers_recent ON peers (engine, last_at DESC);
CREATE TABLE IF NOT EXISTS marks (
    engine TEXT NOT NULL,
    key    TEXT NOT NULL,
    at     REAL NOT NULL,
    PRIMARY KEY (engine, key)
);
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

ADDED_COLUMNS = {
    'take_at': 'REAL NOT NULL DEFAULT 0',
    'gap_n': 'INTEGER NOT NULL DEFAULT 0',
    'gap_sum': 'REAL NOT NULL DEFAULT 0',
    'gap_sq': 'REAL NOT NULL DEFAULT 0',
    'burst': 'INTEGER NOT NULL DEFAULT 0',
}
"""Columns added to ``peers`` after the table had already shipped.

``CREATE TABLE IF NOT EXISTS`` does nothing to a database that exists, so a
deployed ``peers.db`` would keep the original seven columns and the first
bump naming a new one would fail. Adding what is missing is the migration.
"""

JOURNAL = 'DELETE'
"""How SQLite journals a write -- deliberately NOT the WAL default.

WAL keeps recent writes in a sibling ``peers.db-wal`` until a checkpoint, so
the ``.db`` alone is stale, sometimes by everything: the watchdog ends this
process with ``os._exit``, which never closes the connection, so no
checkpoint ever runs. A backup or a hand copy of ``peers.db`` then carries
a valid database with zero rows, which is exactly as alarming as it sounds
and gives no hint that the data is in the file next door.

WAL buys nothing here to pay for that. It exists for concurrent readers
during a write, and this is one process holding one connection per state
directory -- /status reads through the same object that writes. In DELETE
mode the journal is transient, the two sibling files never appear, and the
``.db`` is complete after every commit. Writes fsync one at a time, which
at a few dozen events a day is not a cost worth measuring.

Switching an existing WAL database is safe and automatic: SQLite
checkpoints it on the mode change and removes the sibling files.
"""

_BUMP_SQL = (  # noqa: S608 -- the column names are COUNTERS, never input
    'INSERT INTO peers (engine, peer_id, {cols}, last_at, take_at) '
    'VALUES (?, ?, {marks}, ?, ?) '
    'ON CONFLICT (engine, peer_id) DO UPDATE SET {sets}, '
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
(an offer, say) can pass 0 and leave it alone.
"""


@dataclass(frozen=True)
class PeerRow:
    """One peer's standing with one engine.

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


@dataclass
class StateStore:
    """Read and write engine state; SQLite is truth, JSON is its twin."""

    db_path: Path | str
    json_path: Path | None = None
    _cursors: dict[str, dict[str, object]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Open the database, apply the schema, and load the cursors."""
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.execute(f'PRAGMA journal_mode={JOURNAL}')
        self._conn.commit()
        self._cursors = self._read_cursors()
        self._dirty = False

    # --- per-peer rows ---------------------------------------------------

    def peer(self, engine: str, peer_id: str) -> PeerRow:
        """Return one peer's row, or an empty row if it has none yet."""
        got = self._conn.execute(
            'SELECT * FROM peers WHERE engine = ? AND peer_id = ?',
            (engine, peer_id),
        ).fetchone()
        return _row(got) if got is not None else PeerRow(peer_id)

    def peers(self, engine: str, limit: int = 0) -> list[PeerRow]:
        """Return an engine's peers, most recently interacted-with first."""
        sql = 'SELECT * FROM peers WHERE engine = ? ORDER BY last_at DESC'
        args: tuple[object, ...] = (engine,)
        if limit > 0:
            sql, args = sql + ' LIMIT ?', (engine, limit)
        return [_row(r) for r in self._conn.execute(sql, args)]

    def bump(  # noqa: PLR0913 -- peer + counts + the two moments read best flat
        self,
        engine: str,
        peer_id: str,
        counts: Mapping[str, float],
        at: float | None = None,
        take_at: float = 0.0,
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

        ``take_at`` is that same moment, but passed ONLY when the bump is an
        engagement of ours. ``last_at`` cannot serve both: it answers "when
        did anything happen with this peer" (which orders the readout and the
        trim), and an offer lands at the very instant the engagement that
        follows it needs to measure back from.
        """
        adds = [counts.get(name, 0) for name in COUNTERS]
        moment = time.time() if at is None else at
        self._conn.execute(
            _BUMP_SQL, (engine, peer_id, *adds, moment, take_at)
        )
        self._commit()

    def remember(self, engine: str, peer_id: str, label: str) -> None:
        """Cache a peer's display name; a blank or id-like label is ignored."""
        if not label or label == peer_id:
            return
        self._conn.execute(
            'INSERT INTO peers (engine, peer_id, label) VALUES (?, ?, ?) '
            'ON CONFLICT (engine, peer_id) DO UPDATE SET '
            'label = excluded.label',
            (engine, peer_id, label),
        )
        self._commit()

    def forget(self, engine: str, peer_id: str) -> None:
        """Drop a peer entirely (it rolled off the tracked set)."""
        self._conn.execute(
            'DELETE FROM peers WHERE engine = ? AND peer_id = ?',
            (engine, peer_id),
        )
        self._commit()

    def trim_peers(self, engine: str, keep: int) -> list[str]:
        """Drop all but the ``keep`` most recent peers; return what went.

        The tracked set is bounded so a long-running account does not carry
        every peer it ever met. The caller clears whatever else it keyed by
        those peers -- the store does not know their mark format.
        """
        if keep <= 0:
            return []
        rows = self._conn.execute(
            'SELECT peer_id FROM peers WHERE engine = ? '
            'ORDER BY last_at DESC LIMIT -1 OFFSET ?',
            (engine, keep),
        ).fetchall()
        dropped = [str(r['peer_id']) for r in rows]
        for peer_id in dropped:
            self.forget(engine, peer_id)
        return dropped

    # --- dedup marks -----------------------------------------------------

    def mark(self, engine: str, key: str) -> bool:
        """Record a dedup key; return whether it was NEW.

        This is the "have we already acted here" question -- a reacted
        (post, person) key, a seen story id. False means the caller has
        already handled it and must not act again.
        """
        cur = self._conn.execute(
            'INSERT OR IGNORE INTO marks (engine, key, at) VALUES (?, ?, ?)',
            (engine, key, time.time()),
        )
        self._commit()
        return cur.rowcount > 0

    def marked(self, engine: str, key: str) -> bool:
        """Whether a dedup key has already been recorded."""
        got = self._conn.execute(
            'SELECT 1 FROM marks WHERE engine = ? AND key = ?', (engine, key)
        ).fetchone()
        return got is not None

    def count_marks(self, engine: str) -> int:
        """How many dedup keys an engine currently holds."""
        got = self._conn.execute(
            'SELECT count(*) FROM marks WHERE engine = ?', (engine,)
        ).fetchone()
        return int(got[0])

    def drop_marks(self, engine: str, prefix: str) -> None:
        """Drop every mark under one prefix (a peer that rolled off)."""
        self._conn.execute(
            'DELETE FROM marks WHERE engine = ? AND key LIKE ?',
            (engine, f'{prefix}%'),
        )
        self._commit()

    def trim_marks(self, engine: str, prefix: str, keep: int) -> None:
        """Keep only the newest ``keep`` marks under one prefix.

        A peer whose stories we have watched for years would otherwise
        accumulate a mark per story forever. 0 keeps them all.
        """
        if keep <= 0:
            return
        self._conn.execute(
            'DELETE FROM marks WHERE engine = ? AND key LIKE ? AND key NOT IN '
            '(SELECT key FROM marks WHERE engine = ? AND key LIKE ? '
            'ORDER BY at DESC LIMIT ?)',
            (engine, f'{prefix}%', engine, f'{prefix}%', keep),
        )
        self._commit()

    def keep_marks(self, engine: str, prefixes: tuple[str, ...]) -> None:
        """Drop an engine's marks that no live prefix covers any more.

        Only the last few posts are ever matched, so a key for a post that
        rolled out of the window can never fire again; pruning keeps the
        table bounded. An empty prefix tuple clears the engine.
        """
        if not prefixes:
            self._conn.execute('DELETE FROM marks WHERE engine = ?', (engine,))
        else:
            keep = ' OR '.join(['key LIKE ?'] * len(prefixes))
            self._conn.execute(
                f'DELETE FROM marks WHERE engine = ? AND NOT ({keep})',  # noqa: S608 -- placeholders, not values
                (engine, *(f'{p}%' for p in prefixes)),
            )
        self._commit()

    # --- cursors ---------------------------------------------------------

    def cursor(self, engine: str) -> dict[str, object]:
        """Return an engine's cursor block (empty when it has none yet)."""
        got = self._cursors.get(engine)
        return dict(got) if isinstance(got, dict) else {}

    def put_cursor(self, engine: str, cursor: Mapping[str, object]) -> None:
        """Replace an engine's cursor block and mark the twin stale."""
        self._cursors[engine] = dict(cursor)
        self._dirty = True
        if SNAPSHOT_EVERY_WRITE:
            self.snapshot()

    # --- the readable twin -----------------------------------------------

    def snapshot(self) -> None:
        """Rebuild the JSON twin from what is in memory and in the database.

        Called on the status tick, at profile teardown, and on demand -- not
        per write. Nothing reads it back except a human, so a few seconds of
        staleness costs nothing and saves the O(N) rewrite.
        """
        if self.json_path is None or not self._dirty:
            return
        tmp = self.json_path.with_suffix('.tmp')
        tmp.write_text(
            json.dumps(self._cursors, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        tmp.replace(self.json_path)
        self._dirty = False

    def close(self) -> None:
        """Flush the twin and release the database handle."""
        self.snapshot()
        self._conn.close()

    # --- internals -------------------------------------------------------

    def _migrate(self) -> None:
        """Add any column this version needs that an older file lacks.

        Idempotent: what is already there is left alone, so an existing
        database keeps every count it has and starts the new ones at zero.
        """
        have = {
            str(row['name'])
            for row in self._conn.execute('PRAGMA table_info(peers)')
        }
        for name, decl in ADDED_COLUMNS.items():
            if name not in have:
                self._conn.execute(
                    f'ALTER TABLE peers ADD COLUMN {name} {decl}'
                )
        self._conn.commit()

    def _commit(self) -> None:
        """Commit the write and note that the twin is now behind."""
        self._conn.commit()
        self._dirty = True

    def _read_cursors(self) -> dict[str, dict[str, object]]:
        """Load the cursor file, or start empty if there is none/it is bad."""
        if self.json_path is None or not self.json_path.exists():
            return {}
        try:
            raw = json.loads(self.json_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            log.warning('state: cursors file unreadable; starting fresh')
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}


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
