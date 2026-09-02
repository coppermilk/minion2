# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""One file holds the bot's whole state; one object hands out a view of it.

The state a service keeps is two shapes with opposite scaling, and that is
why it is stored the way it is:

* PER-PEER rows -- who commented, who was engaged, whose stories were seen.
  These grow with the audience. Modelled at 300 peers the hand-rolled JSON
  files came to 163 KB / 4 425 lines and 199 KB / 14 423 lines, and every
  one of the fifteen call sites that touched state rewrote its whole file: a
  heartbeat storing one number rewrote 163 KB, 1440 times a day. Here an
  event writes ONE ROW, whatever the audience size.
* SCALAR STATE -- mood, the session marks, the daily counters, the poster's
  ledger. Bounded forever, and nothing in it is worth querying, so it is one
  JSON blob in one row.

Both live in ONE DATABASE, ``userbot.db``, per profile directory. Not one
per service: six files named for six services is the same zoo as before with
tidier labels, and it makes the obvious question -- what does this account
know -- into six queries against six connections.

What made the shared file bad the first time was not the sharing, it was the
KEY. Every one of sixteen methods took the engine name as its first argument,
so every call site could pass the wrong one and nothing would notice. Here
the key is bound ONCE: ``Database.store('stories')`` returns a view that
already knows its name, and not one method takes it. The callers read exactly
as they did when each service owned a file, and the SQL is the only place the
column exists.

``engines/users.py`` puts its own tables in the same file through the same
connection. It is not a service with a ledger -- it is the audience, one per
profile -- so it has no row in ``state`` and no name to bind.
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

DB_NAME = 'userbot.db'
"""The one state file, per profile directory (live and test each get one)."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS peers (
    service TEXT    NOT NULL,
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
    PRIMARY KEY (service, peer_id)
);
CREATE INDEX IF NOT EXISTS peers_recent ON peers (service, last_at DESC);
CREATE TABLE IF NOT EXISTS marks (
    service TEXT NOT NULL,
    key     TEXT NOT NULL,
    at      REAL NOT NULL,
    PRIMARY KEY (service, key)
);
CREATE TABLE IF NOT EXISTS state (
    service TEXT PRIMARY KEY,
    blob    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    username   TEXT,
    first_name TEXT,
    last_name  TEXT,
    phone      TEXT,
    first_seen REAL,
    last_seen  REAL,
    msg_count  INTEGER NOT NULL DEFAULT 0,
    subscribed INTEGER NOT NULL DEFAULT 0,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS membership_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    event        TEXT    NOT NULL,
    ts           REAL    NOT NULL,
    admin_log_id INTEGER UNIQUE
);
CREATE TABLE IF NOT EXISTS messages (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    msg_id  INTEGER NOT NULL,
    root    INTEGER,
    text    TEXT,
    ts      REAL    NOT NULL,
    UNIQUE (chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS ix_membership_user ON membership_events (user_id);
CREATE INDEX IF NOT EXISTS ix_messages_user ON messages (user_id);
"""
"""Every table in the file, declared once.

The first three are per-service and carry the name in a column. ``state`` is
deliberately ONE table for what used to be five: the story and like engines
called their blob a "cursor block"; the poster, the greeter and the cabinet
called theirs a "state file" and hand-rolled a JSON file each, under three
different naming conventions. One thing wearing five costumes -- a service's
scalar state, as JSON, one row.

The last three are the AUDIENCE, and they have no service column because
they have no service: one channel's members and messages belong to the
profile, not to whoever happened to write the row. ``engines/users.py``
holds every query against them and none of their DDL -- the schema of the
one database is this string, so there is one place to read it.
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

WAL keeps recent writes in a sibling ``userbot.db-wal`` until a checkpoint,
so the ``.db`` alone is stale, sometimes by everything: the watchdog ends this
process with ``os._exit``, which never closes the connection, so no checkpoint
ever runs. A backup or a hand copy of the database then carries a valid file
with zero rows in it, which is exactly as alarming as it sounds and gives no
hint that the data is in the file next door.

WAL buys nothing here to pay for that. It exists for concurrent readers during
a write, and this is one process holding one connection per profile directory
-- /status reads through the same connection that writes. In DELETE mode the
journal is transient, the two sibling files never appear, and the ``.db`` is
complete after every commit. Writes fsync one at a time, which at a few dozen
events a day is not a cost worth measuring.

Switching an existing WAL database is safe and automatic: SQLite checkpoints
it on the mode change and removes the sibling files.

The Telegram session next to us makes the OPPOSITE choice on purpose, and
neither is wrong (see ``minion_core/adapters/userchat.connect``). It is not
ours to write: Telethon commits to it on its own schedule, from inside
library code, so a kill can land mid-commit -- and on the NAS mount that
left the file malformed and forced a re-login every couple of weeks, which
WAL fixed. Every write to THIS file is one statement we make and commit, so
there is no mid-commit to land in, and being self-contained on disk is worth
more than recovering from a crash that cannot happen here.
"""

_BUMP_SQL = (  # noqa: S608 -- the column names are COUNTERS, never input
    'INSERT INTO peers (service, peer_id, {cols}, last_at, take_at) '
    'VALUES (?, ?, {marks}, ?, ?) '
    'ON CONFLICT (service, peer_id) DO UPDATE SET {sets}, '
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
    """One peer's standing with one service.

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


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a state database, applying the schema and the journal mode.

    Public because ``engines/users.py`` opens the same file for its own
    tables in tests; in the running bot everyone shares ``Database``'s.
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.execute(f'PRAGMA journal_mode={JOURNAL}')
    conn.commit()
    return conn


@dataclass
class Database:
    """The one state file, and the views onto it.

    One per profile directory: live and test each get their own, exactly as
    the JSON files did, so a sandboxed service never writes where a live one
    reads.
    """

    path: Path | str

    def __post_init__(self) -> None:
        """Open the file and apply the schema."""
        self.conn = connect(self.path)

    def store(self, service: str) -> StateStore:
        """Return one service's view, bound to its name.

        The view is two fields and no state of its own, so callers may hold
        one or ask again; both see the same rows.
        """
        return StateStore(self.conn, service)

    def close(self) -> None:
        """Release the database handle."""
        self.conn.close()


@dataclass(frozen=True)
class StateStore:
    """One service's view of the state database.

    Bound to its name at construction, which is the whole design: not one
    method below takes a service argument, so no call site can name the
    wrong one. It reads as though this service owned the file.
    """

    conn: sqlite3.Connection
    service: str

    # --- per-peer rows ---------------------------------------------------

    def peer(self, peer_id: str) -> PeerRow:
        """Return one peer's row, or an empty row if it has none yet."""
        got = self.conn.execute(
            'SELECT * FROM peers WHERE service = ? AND peer_id = ?',
            (self.service, peer_id),
        ).fetchone()
        return _row(got) if got is not None else PeerRow(peer_id)

    def peers(self, limit: int = 0) -> list[PeerRow]:
        """Return this service's peers, most recently engaged first."""
        sql = 'SELECT * FROM peers WHERE service = ? ORDER BY last_at DESC'
        args: tuple[object, ...] = (self.service,)
        if limit > 0:
            sql, args = sql + ' LIMIT ?', (self.service, limit)
        return [_row(r) for r in self.conn.execute(sql, args)]

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
        self.conn.execute(
            _BUMP_SQL, (self.service, peer_id, *adds, moment, took)
        )
        self.conn.commit()

    def remember(self, peer_id: str, label: str) -> None:
        """Cache a peer's display name; a blank or id-like label is ignored."""
        if not label or label == peer_id:
            return
        self.conn.execute(
            'INSERT INTO peers (service, peer_id, label) VALUES (?, ?, ?) '
            'ON CONFLICT (service, peer_id) DO UPDATE SET '
            'label = excluded.label',
            (self.service, peer_id, label),
        )
        self.conn.commit()

    def forget(self, peer_id: str) -> None:
        """Drop a peer entirely (it rolled off the tracked set)."""
        self.conn.execute(
            'DELETE FROM peers WHERE service = ? AND peer_id = ?',
            (self.service, peer_id),
        )
        self.conn.commit()

    def trim_peers(self, keep: int) -> list[str]:
        """Drop all but the ``keep`` most recent peers; return what went.

        The tracked set is bounded so a long-running account does not carry
        every peer it ever met. The caller clears whatever else it keyed by
        those peers -- the store does not know their mark format.
        """
        if keep <= 0:
            return []
        rows = self.conn.execute(
            'SELECT peer_id FROM peers WHERE service = ? '
            'ORDER BY last_at DESC LIMIT -1 OFFSET ?',
            (self.service, keep),
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
        cur = self.conn.execute(
            'INSERT OR IGNORE INTO marks (service, key, at) VALUES (?, ?, ?)',
            (self.service, key, time.time()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def marked(self, key: str) -> bool:
        """Whether a dedup key has already been recorded."""
        got = self.conn.execute(
            'SELECT 1 FROM marks WHERE service = ? AND key = ?',
            (self.service, key),
        ).fetchone()
        return got is not None

    def count_marks(self) -> int:
        """How many dedup keys this service currently holds."""
        got = self.conn.execute(
            'SELECT count(*) FROM marks WHERE service = ?', (self.service,)
        ).fetchone()
        return int(got[0])

    def drop_marks(self, prefix: str) -> None:
        """Drop every mark under one prefix (a peer that rolled off)."""
        self.conn.execute(
            'DELETE FROM marks WHERE service = ? AND key LIKE ?',
            (self.service, f'{prefix}%'),
        )
        self.conn.commit()

    def trim_marks(self, prefix: str, keep: int) -> None:
        """Keep only the newest ``keep`` marks under one prefix.

        A peer whose stories we have watched for years would otherwise
        accumulate a mark per story forever. 0 keeps them all.
        """
        if keep <= 0:
            return
        self.conn.execute(
            'DELETE FROM marks WHERE service = ? AND key LIKE ? '
            'AND key NOT IN (SELECT key FROM marks WHERE service = ? '
            'AND key LIKE ? ORDER BY at DESC LIMIT ?)',
            (self.service, f'{prefix}%', self.service, f'{prefix}%', keep),
        )
        self.conn.commit()

    def keep_marks(self, prefixes: tuple[str, ...]) -> None:
        """Drop this service's marks that no live prefix covers any more.

        Only the last few posts are ever matched, so a key for a post that
        rolled out of the window can never fire again; pruning keeps the
        table bounded. An empty prefix tuple clears the service.
        """
        if not prefixes:
            self.conn.execute(
                'DELETE FROM marks WHERE service = ?', (self.service,)
            )
        else:
            keep = ' OR '.join(['key LIKE ?'] * len(prefixes))
            self.conn.execute(
                f'DELETE FROM marks WHERE service = ? AND NOT ({keep})',  # noqa: S608 -- placeholders, not values
                (self.service, *(f'{p}%' for p in prefixes)),
            )
        self.conn.commit()

    # --- the scalar state block ------------------------------------------

    def read(self) -> dict[str, object]:
        """Return this service's state block, or ``{}`` if it has none.

        Missing and unreadable both mean "start from your defaults". A caller
        for whom that would silently discard history calls ``read_strict``.
        """
        got = self.conn.execute(
            'SELECT blob FROM state WHERE service = ?', (self.service,)
        ).fetchone()
        if got is None:
            return {}
        try:
            raw = json.loads(got[0])
        except ValueError:
            log.warning('state: %s has an unreadable block', self.service)
            return {}
        return dict(raw) if isinstance(raw, dict) else {}

    def read_strict(self) -> dict[str, object]:
        """Return this service's state block, raising rather than degrading.

        For the caller whose empty result would be a LIE: the poster reading
        "nothing was ever posted" disarms its re-post guard and republishes
        the backlog. Better to fail loudly and let the watchdog restart us.
        """
        got = self.conn.execute(
            'SELECT blob FROM state WHERE service = ?', (self.service,)
        ).fetchone()
        if got is None:
            return {}
        data = json.loads(got[0])
        if not isinstance(data, dict):
            msg = f'{self.service}: state is not an object'
            raise TypeError(msg)
        return data

    def write(self, data: Mapping[str, object]) -> None:
        """Replace this service's state block; the transaction is atomicity.

        A kill mid-write leaves the previous row whole, which is what the old
        write-a-temp-file-and-rename dance bought by hand. Written through on
        every call rather than coalesced into a periodic rebuild: one small
        row costs what a bump costs, and the watchdog's ``os._exit`` leaves
        nothing waiting to be flushed. The JSON file this replaces had to be
        rewritten whole, which is why it could not be.
        """
        self.conn.execute(
            'INSERT INTO state (service, blob) VALUES (?, ?) '
            'ON CONFLICT (service) DO UPDATE SET blob = excluded.blob',
            (self.service, json.dumps(data, ensure_ascii=False)),
        )
        self.conn.commit()


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


# ------------------------------------------------- adopting the older shapes

LEGACY_CURSORS = 'cursors.json'
"""The shared cursor file: one JSON block per engine, keyed by name."""

LEGACY_JSON = {
    'aggregator_state.json': 'aggregator',
    'greeter_state.json': 'greeter',
    'comod.json': 'comod',
}
"""The hand-rolled JSON files, and whose state each one was.

A list of what used to exist, which the ``.db`` half of the import
deliberately does not need -- it reads a file's shape off its tables. JSON
has no tables to read, so the name is the only thing that says whose state
this is, and three services picked three conventions between them.

They are here because an install can skip a release. The version before last
imported these into per-service databases and renamed them ``.bak``; an
install that jumps straight from before that to here would find them
untouched, and dropping the path would quietly start it from zero.
"""

PEER_COLUMNS = tuple(f.name for f in fields(PeerRow))
MARK_COLUMNS = ('key', 'at')
AUDIENCE_TABLES = ('users', 'membership_events', 'messages')
"""What a row is worth carrying over, named once so the import cannot drift
from the schema above."""


def adopt(where: Path) -> None:
    """Fold every older state file in a directory into the one database.

    Three shapes have shipped and any of them may be on disk:

    * a hand-rolled JSON file per service, under three naming conventions;
    * ``peers.db`` + ``cursors.json`` -- one shared pair, keyed by an engine
      column and an engine key;
    * ``<service>.db`` -- one file per service, six of them, each holding
      either a ledger (peers/marks/cursor) or a single JSON block (state);
    * ``users.db`` -- the audience, which was never a service at all.

    Which one a file is, is read off its TABLES rather than its name, so the
    import does not carry a list of what used to exist; the name only says
    which service the rows belong to. Everything lands in ``userbot.db`` and
    the file it came from is renamed aside, so this runs once and a restart
    cannot re-import a stale file over newer state.

    What the one database already holds wins -- the import adds, it never
    overwrites -- and nothing is renamed unless its import got through.

    Says out loud what it took. A migration that runs once, silently, on a
    machine nobody is watching is one you can only audit by opening the
    database and counting -- which is exactly what the first person to meet
    an unexpected file ends up doing.
    """
    conn = connect(where / DB_NAME)
    try:
        older = [p for p in sorted(where.glob('*.db')) if p.name != DB_NAME]
        taken = [(p, _absorb(conn, p)) for p in older]
        taken.append(
            (
                where / LEGACY_CURSORS,
                _absorb_cursors(conn, where / LEGACY_CURSORS),
            )
        )
        taken += [
            (where / name, _absorb_json(conn, where / name, service))
            for name, service in LEGACY_JSON.items()
        ]
    finally:
        conn.close()
    done = [path for path, imported in taken if imported]
    for path in done:
        _retire(path)
    log.info(
        'state: %s holds %s',
        DB_NAME,
        f'{len(done)} adopted file(s): {", ".join(p.name for p in done)}'
        if done
        else 'its own state; nothing older found to adopt',
    )


def _absorb(conn: sqlite3.Connection, path: Path) -> bool:
    """Take everything one older database holds; say whether anything came.

    Opening it is also what folds a WAL install back together: SQLite
    checkpoints and removes the ``-wal`` and ``-shm`` siblings when the last
    connection closes cleanly, which the killed process that wrote them never
    did. So they are gone before the file is renamed aside, instead of
    outliving it under a name that matches nothing left in the directory --
    while holding the only copy of the rows.
    """
    try:
        conn.execute('ATTACH DATABASE ? AS old', (str(path),))
    except sqlite3.Error:
        log.exception('state: %s cannot be read; not adopting it', path)
        return False
    try:
        service = path.stem
        ledger = _absorb_ledger(conn, service)
        block = _absorb_block(conn, service)
        audience = _absorb_audience(conn)
        conn.commit()
    except sqlite3.Error:
        log.exception('state: %s failed to import; leaving it in place', path)
        return False
    finally:
        conn.execute('DETACH DATABASE old')
    return ledger or block or audience


def _absorb_ledger(conn: sqlite3.Connection, service: str) -> bool:
    """Copy the attached database's peer rows and dedup marks.

    The service name is the ``engine`` column when the old file had one (the
    shared database every engine wrote to) and the caller's otherwise (the
    per-service files that replaced it): one query, two places the key can
    come from. Only the columns both schemas have are carried, so a file from
    before the timing columns existed leaves those at their defaults.
    """
    took = False
    for table, wanted in (('peers', PEER_COLUMNS), ('marks', MARK_COLUMNS)):
        have = _columns(conn, table)
        names = ', '.join(name for name in wanted if name in have)
        if not names:
            continue
        key = 'engine' if 'engine' in have else '?'
        conn.execute(
            f'INSERT OR IGNORE INTO {table} (service, {names}) '  # noqa: S608 -- schema names, not input
            f'SELECT {key}, {names} FROM old.{table}',
            () if key == 'engine' else (service,),
        )
        took = True
    return took


def _absorb_block(conn: sqlite3.Connection, service: str) -> bool:
    """Copy the attached database's one JSON block under the file's name.

    Two table names carried the same single row -- ``cursor`` in a ledger
    file, ``state`` in a blob file -- and both become a ``state`` row here. A
    ``state`` table that already has a ``service`` column is this schema, not
    an older one, and is left alone.
    """
    for table in ('state', 'cursor'):
        have = _columns(conn, table)
        if 'blob' not in have or 'service' in have:
            continue
        got = conn.execute(f'SELECT blob FROM old.{table}').fetchone()  # noqa: S608 -- a literal, not input
        if got is None:
            continue
        conn.execute(
            'INSERT OR IGNORE INTO state (service, blob) VALUES (?, ?)',
            (service, got[0]),
        )
        return True
    return False


def _absorb_audience(conn: sqlite3.Connection) -> bool:
    """Copy the attached database's audience tables, keys and all.

    No service name is involved: these rows are the channel's members and
    their messages, which belong to the profile. That is the whole reason
    ``users.db`` stops being a file of its own.
    """
    took = False
    for table in AUDIENCE_TABLES:
        names = ', '.join(sorted(_columns(conn, table)))
        if not names:
            continue
        conn.execute(
            f'INSERT OR IGNORE INTO {table} ({names}) '  # noqa: S608 -- schema names, not input
            f'SELECT {names} FROM old.{table}'
        )
        took = True
    return took


def _absorb_cursors(conn: sqlite3.Connection, path: Path) -> bool:
    """Copy the shared cursors file: one JSON block per engine, by name."""
    blocks = _read_cursors(path)
    if blocks is None:
        return False
    for service, block in blocks.items():
        conn.execute(
            'INSERT OR IGNORE INTO state (service, blob) VALUES (?, ?)',
            (service, json.dumps(block, ensure_ascii=False)),
        )
    conn.commit()
    return True


def _absorb_json(conn: sqlite3.Connection, path: Path, service: str) -> bool:
    """Copy one service's hand-rolled JSON file into its state row."""
    if not path.exists():
        return False
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        log.warning('state: %s unreadable; its state is lost', path.name)
        return False
    if not isinstance(raw, dict):
        return False
    conn.execute(
        'INSERT OR IGNORE INTO state (service, blob) VALUES (?, ?)',
        (service, json.dumps(raw, ensure_ascii=False)),
    )
    conn.commit()
    return True


def _read_cursors(path: Path) -> dict[str, dict[str, object]] | None:
    """Parse the shared cursors file; None when there is nothing to take."""
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        log.warning('state: %s unreadable; its cursors are lost', path.name)
        return None
    if not isinstance(raw, dict):
        return None
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the columns the attached OLD database's table has, if any."""
    return {
        str(row['name'])
        for row in conn.execute(f'PRAGMA old.table_info({table})')
    }


JOURNAL_SIBLINGS = ('-journal', '-wal', '-shm')
"""The files SQLite leaves beside a database it did not close cleanly."""


def _retire(path: Path) -> None:
    """Rename an imported file aside, so it can only be imported once.

    Any journal sibling still on disk goes with it. It cannot be holding
    anything we need: the import opened the file and closed it, which is
    when SQLite rolls a journal back or checkpoints a WAL, and everything
    the file held has already been copied. Left behind it would sit in the
    directory under a name that matches nothing -- the exact clutter this
    whole move is undoing.
    """
    path.rename(path.with_suffix(path.suffix + '.bak'))
    for tail in JOURNAL_SIBLINGS:
        path.with_name(path.name + tail).unlink(missing_ok=True)
