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
from datetime import datetime
from typing import TYPE_CHECKING
from typing import TypeGuard

if TYPE_CHECKING:
    from collections.abc import Iterable
    from collections.abc import Mapping
    from pathlib import Path

log = logging.getLogger('userbot')

DB_NAME = 'userbot.db'
"""The one state file, per profile directory (live and test each get one)."""

RUNGS = ('offered', 'taken', 'recip')
"""The three steps of any engagement, weakest first -- the MODEL's names.

They are the first three ``COUNTERS`` by design, so a counter and the log
rows it totals stay tied to each other. What each step is CALLED, though,
depends on the service: see ``ACTS``.
"""

ACTS = {
    'reactions': ('ignore', 'like', 'sticker'),
    'stories': ('ignore', 'seen', 'like'),
}
"""What the person on the other side would call each rung of ``RUNGS``.

One entry per service that keeps a ledger, listed weakest first and lined up
with ``RUNGS`` position by position:

===========  ==========  =========  ==========
service      offered     taken      recip
===========  ==========  =========  ==========
reactions    ``ignore``  ``like``   ``sticker``
stories      ``ignore``  ``seen``   ``like``
===========  ==========  =========  ==========

``offered`` is a chance we did not take, so from outside it IS the ignore --
the same row, named for what happened rather than for what was possible.
Naming the rungs per service is the difference between a history that reads
``taken #405`` and one that reads ``seen #405``: the same fact, but only the
second says what the other person actually saw. And they are not the same
act across services -- a like costs nothing on a comment and is the strongest
thing we ever do to a story -- which a single shared vocabulary would hide.

Every rung is spelled out here rather than derived, because the CHECK below
is built from this table: a service can only log the acts it has words for,
and a typo becomes an IntegrityError instead of a row nobody ever reads.
"""

SERVICES = ('aggregator', 'reactions', 'stories', 'greeter', 'comod')
"""Every service that owns rows in this file, and the only legal ``service``.

Deliberately NOT ``glue/commands.SERVICE_NAMES``, which is the list of tap
commands: that one carries ``users`` (which has no service column -- the
audience belongs to the profile) and lacks ``comod`` (which has no on/off
switch but does keep a cabinet). Two lists because they answer two
questions; sharing one would make each wrong about the other.

The CHECK below is generated from this, so a service name that does not
exist becomes an IntegrityError at the write rather than a partition of the
database that nobody ever reads back.
"""

_SERVICE_CHECK = 'service IN ({})'.format(
    ', '.join(f"'{name}'" for name in SERVICES)
)
"""The ``service`` CHECK, generated from ``SERVICES`` so it cannot drift."""

_ACT_CHECK = '\n        OR '.join(
    "(service = '{name}' AND act IN ({acts}))".format(
        name=name, acts=', '.join(f"'{act}'" for act in ladder)
    )
    for name, ladder in ACTS.items()
)
"""The ``contact.act`` CHECK, generated from ``ACTS`` so it cannot drift."""

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS actors (
    peer_id    INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT '',
    username   TEXT NOT NULL DEFAULT '',
    title      TEXT NOT NULL DEFAULT '',
    first_name TEXT NOT NULL DEFAULT '',
    last_name  TEXT NOT NULL DEFAULT '',
    phone      TEXT NOT NULL DEFAULT '',
    first_seen REAL NOT NULL DEFAULT 0,
    last_seen  REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audience (
    peer_id    INTEGER PRIMARY KEY,
    subscribed INTEGER NOT NULL DEFAULT 0,
    msg_count  INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (peer_id) REFERENCES actors (peer_id)
);
CREATE TABLE IF NOT EXISTS standing (
    service TEXT    NOT NULL,
    peer_id INTEGER NOT NULL,
    offered INTEGER NOT NULL DEFAULT 0,
    taken   INTEGER NOT NULL DEFAULT 0,
    recip   INTEGER NOT NULL DEFAULT 0,
    last_at REAL    NOT NULL DEFAULT 0,
    take_at REAL    NOT NULL DEFAULT 0,
    gap_n   INTEGER NOT NULL DEFAULT 0,
    gap_sum REAL    NOT NULL DEFAULT 0,
    gap_sq  REAL    NOT NULL DEFAULT 0,
    burst   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (service, peer_id),
    FOREIGN KEY (peer_id) REFERENCES actors (peer_id),
    CHECK ({_SERVICE_CHECK})
);
CREATE TABLE IF NOT EXISTS contact (
    id      INTEGER PRIMARY KEY,
    peer_id INTEGER NOT NULL,
    at      REAL    NOT NULL,
    service TEXT    NOT NULL,
    act     TEXT    NOT NULL,
    subject INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (peer_id) REFERENCES actors (peer_id),
    CHECK ({_ACT_CHECK})
);
CREATE TABLE IF NOT EXISTS marks (
    service TEXT NOT NULL,
    key     TEXT NOT NULL,
    at      REAL NOT NULL,
    PRIMARY KEY (service, key),
    CHECK ({_SERVICE_CHECK})
);
CREATE TABLE IF NOT EXISTS uptime (
    hour   INTEGER PRIMARY KEY,
    weight REAL    NOT NULL DEFAULT 0,
    at     REAL    NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS scheduled (
    id       INTEGER PRIMARY KEY,
    service  TEXT    NOT NULL,
    chat     INTEGER NOT NULL,
    reply_to INTEGER NOT NULL,
    root     INTEGER NOT NULL DEFAULT 0,
    due_at   REAL    NOT NULL,
    kind     TEXT    NOT NULL DEFAULT '',
    text     TEXT    NOT NULL DEFAULT '',
    emojis   TEXT    NOT NULL DEFAULT '[]',
    UNIQUE (service, chat, reply_to),
    CHECK ({_SERVICE_CHECK})
);
CREATE TABLE IF NOT EXISTS watching (
    service TEXT    NOT NULL,
    chat_id INTEGER NOT NULL,
    post_id INTEGER NOT NULL,
    at      REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (service, chat_id, post_id),
    CHECK ({_SERVICE_CHECK})
);
CREATE TABLE IF NOT EXISTS emoji_used (
    service  TEXT NOT NULL,
    emoji_id TEXT NOT NULL,
    at       REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (service, emoji_id),
    CHECK ({_SERVICE_CHECK})
);
CREATE TABLE IF NOT EXISTS posted (
    id      INTEGER PRIMARY KEY,
    title   TEXT NOT NULL,
    at      REAL NOT NULL DEFAULT 0,
    links   TEXT NOT NULL DEFAULT '{{}}',
    msg_ids TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS pending (
    id      INTEGER PRIMARY KEY,
    title   TEXT NOT NULL,
    since   REAL NOT NULL DEFAULT 0,
    items   TEXT NOT NULL DEFAULT '{{}}',
    msg_ids TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS rejected (
    id    INTEGER PRIMARY KEY,
    title TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cabinet (
    nick   TEXT PRIMARY KEY,
    at     REAL NOT NULL DEFAULT 0,
    amount TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS state (
    service TEXT PRIMARY KEY,
    blob    TEXT NOT NULL,
    CHECK ({_SERVICE_CHECK})
);
CREATE TABLE IF NOT EXISTS membership_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_id      INTEGER NOT NULL,
    event        TEXT    NOT NULL,
    ts           REAL    NOT NULL,
    admin_log_id INTEGER UNIQUE,
    FOREIGN KEY (peer_id) REFERENCES actors (peer_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    msg_id  INTEGER NOT NULL,
    root    INTEGER,
    text    TEXT,
    ts      REAL    NOT NULL,
    UNIQUE (chat_id, msg_id),
    FOREIGN KEY (peer_id) REFERENCES actors (peer_id)
);
"""
"""Every table in the file, declared once.

``actors`` is WHO -- one row per person or channel, keyed by the Telegram id
as a NUMBER. It is the only place a name is stored. There used to be two: a
``users`` table with structured fields for the audience, and a ``label``
column on the ledger holding ``'@eliza (360724480)'`` for everyone else --
the same person twice, under two ids of two different types (TEXT against
INTEGER), which could not be joined even in principle. And the label was a
rendered string, so the display layer had to un-render it
(``status.py`` stripped the id back off by exact suffix) to show a name.
Storage keeps fields; rendering makes strings; the two stop meeting.

``contact`` is WHAT HAPPENED -- one append-only row per act, named in the
service's own vocabulary (``ACTS``), which makes it the history of our
relationship with a person and the thing ``standing`` is a running total OF.
That is the point: the counters used to be the only copy of the model's
inputs, so a surprising number (why is exposure 44%?) had nothing behind it
to check against, and answering took a simulation rather than a query.

Every chance leaves EXACTLY ONE row saying what we did with it -- ``ignore``
or the rung we reached -- so the log is a list of decisions rather than a
list of opportunities with the decisions filed elsewhere. A reciprocation
adds a second row on top, because it is a second moment: a story is seen,
and then, later, hearted.

``audience`` and ``standing`` are both AGGREGATES over that one actor:
membership of our channel, and our relationship with them per service.

``uptime``, ``scheduled``, ``watching`` and ``emoji_used`` are a service's
COLLECTIONS. The rule that put them here: a column holds one value, a plural
is a table, and the blob keeps only scalars. It is not tidiness. Each of
these lived inside the JSON blob, and a blob is rewritten WHOLE on every
touch -- so the heartbeat, storing one number sixty times an hour, rewrote
the reactions block (~11 KB with a real audience) each time: 15 MB a day to
record ``+1``. ``uptime`` makes that one row.

``state`` is what is left: a service's SCALAR state as JSON, one row --
deliberately ONE table for what used to be five hand-rolled files under
three naming conventions. A test walks every block of a filled database and
fails on any value that is a list or a dict, so the rule stays a fact about
the schema rather than a habit of whoever last edited a ``_save``.
"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS standing_recent ON standing (service, last_at DESC);
CREATE INDEX IF NOT EXISTS contact_when ON contact (peer_id, at DESC);
CREATE INDEX IF NOT EXISTS ix_membership_peer ON membership_events (peer_id);
CREATE INDEX IF NOT EXISTS ix_messages_peer ON messages (peer_id);
"""
"""The indexes, apart from the tables because they are built LATER.

An index names columns, so it can only be built once every table is in this
shape -- and a deployed file is not, until ``_migrate`` has folded it. Built
with the tables instead, the first open of an older file would fail on a
column the fold was about to create.
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

_REGISTERS = {
    'posted': ('title', 'at', 'links', 'msg_ids'),
    'pending': ('title', 'since', 'items', 'msg_ids'),
    'rejected': ('title',),
}
"""The poster's three registers, and the columns each one carries.

Named here rather than passed in, because the table name reaches SQL as a
STRING -- an identifier cannot be a bound parameter. Looking it up in this
table is what makes that safe: a caller can only ask for a register that was
declared here, so nothing a caller says ever becomes SQL.

``links``, ``items`` and ``msg_ids`` stay JSON on purpose. They are the
platform->url map, the platform->item map and the source message ids OF ONE
PUBLICATION -- composite values belonging to their row, not collections of
entities anything would query across. The rule is about plurals that grow
with the world, and these do not.
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
    'INSERT INTO standing (service, peer_id, {cols}, last_at, take_at) '
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

_ALIVE_STEP = 1.0
"""One observation added to an hour bucket per heartbeat."""

_FOREVER = 1e18
"""Stand-in half-life when decay is switched off (0): nothing ever fades."""

IDENTITY = ('kind', 'username', 'title', 'first_name', 'last_name', 'phone')
"""The fields that say WHO a peer is.

All of them combine the same way: a blank one keeps whatever we already
knew. Telegram answers a resolution with whatever it feels like sharing --
a username for one call, only a first name for the next -- so a later,
thinner answer must never erase a fuller one.
"""

ACTOR_SQL = (  # noqa: S608 -- the column names are IDENTITY, never input
    'INSERT INTO actors (peer_id, {cols}, first_seen, last_seen, updated_at) '
    'VALUES (?, {marks}, ?, ?, ?) '
    'ON CONFLICT (peer_id) DO UPDATE SET {sets}, '
    'last_seen = excluded.last_seen, updated_at = excluded.updated_at'
).format(
    cols=', '.join(IDENTITY),
    marks=', '.join('?' * len(IDENTITY)),
    sets=', '.join(
        f"{name} = coalesce(nullif(excluded.{name}, ''), {name})"
        for name in IDENTITY
    ),
)
"""One upsert over every identity field, built from the list above.

Public because ``engines/users.py`` writes an actor too -- the audience
enricher learns the same fields from a different door, and two upserts
would be two chances to combine them differently.

``first_seen`` is written only by the INSERT, so it keeps the first moment
we ever met this peer; ``last_seen`` is overwritten every time.
"""


@dataclass(frozen=True)
class Actor:
    """One person or channel, in the fields Telegram gives us for them.

    Fields, not a rendered name: ``'@eliza (360724480)'`` is a sentence
    about an actor, and which half of it to show depends on who is reading
    (the routing list wants the id, a list of people does not). Composing
    that sentence is the render layer's job -- see ``core/render.py``. When
    it was stored composed, the render layer had to take it apart again.

    ``kind`` is 'user' or 'chat', which is all the bot ever needs to know
    about the difference; both live here because Telegram gives them one
    id namespace and the story engine watches both.
    """

    peer_id: int
    kind: str = ''
    username: str = ''
    title: str = ''  # a channel's name
    first_name: str = ''
    last_name: str = ''
    phone: str = ''  # only ever set for a mutual contact


@dataclass(frozen=True)
class Contact:
    """One touch between us and a peer: what happened, when, about what.

    ``subject`` is the thing the act was about -- a story id, a comment's
    message id -- so the log says WHICH story we opened, not merely that we
    opened one. 0 when the caller has nothing to name, which is honest
    rather than absent: the act still happened and still counts.
    """

    peer_id: int
    at: float
    act: str
    subject: int = 0


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

    No name here: who this peer IS lives once, in ``actors``.
    """

    peer_id: int
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
    _migrate(conn)  # bring an older file to this shape...
    conn.executescript(_INDEXES)  # ...before indexing it
    conn.commit()
    # LAST, and that order is the point: SQLite checks foreign keys as rows
    # are written, so the fold above -- which moves rows between tables in a
    # deliberate order -- runs with them off, and everything the running bot
    # writes runs with them on.
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Fold this file's OLDER shape into the current one, once.

    ``CREATE TABLE IF NOT EXISTS`` does nothing to a database that already
    exists, so a deployed file keeps whatever tables it was made with. This
    is where a shape change lands -- ``adopt`` is for OTHER files, this is
    for ours.

    One transaction: either the whole fold happens or the old tables are
    still there to try again next start.
    """
    old = {
        str(row['name'])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('peers', 'users')"
        )
    }
    if old:
        with conn:
            if 'peers' in old:
                _fold_peers(conn)
            if 'users' in old:
                _fold_users(conn)
            for table in old:
                conn.execute(f'DROP TABLE {table}')
        log.info(
            'state: folded %s into actors/standing', ', '.join(sorted(old))
        )
    _drain_blobs(conn)
    _adopt_orphans(conn)


_REFERRERS = (
    'standing',
    'contact',
    'audience',
    'membership_events',
    'messages',
)
"""Every table whose ``peer_id`` points at ``actors``."""


def _adopt_orphans(conn: sqlite3.Connection) -> None:
    """Give every peer we have rows about an ``actors`` row, if it lacks one.

    A deployed file predates the foreign key, and ``CREATE TABLE IF NOT
    EXISTS`` cannot add one to a table that is already there -- so the
    constraint is declared for new files while THIS is what makes the claim
    true of old ones. Rebuilding five tables to attach the key would be a far
    riskier way to reach the same place.

    An adopted row carries nothing but the id: who they are is unknown until
    somebody resolves them, and inventing a blank name would be worse than
    admitting we only have a number.
    """
    made = 0
    with conn:
        for table in _REFERRERS:
            cur = conn.execute(
                f'INSERT OR IGNORE INTO actors (peer_id) '  # noqa: S608
                f'SELECT DISTINCT peer_id FROM {table} WHERE peer_id NOT IN '
                '(SELECT peer_id FROM actors)'
            )
            made += max(0, cur.rowcount)
    if made:
        log.info('state: adopted %d peer(s) that had rows but no actor', made)


_BLOB_KEYS = (
    'alive',
    'queue',
    'posts',
    'emoji_last',
    'log',
    'session',
    'left',
    'posted',
    'pending',
    'rejected',
    'groups',
    'processed_ids',
)
"""The collections that used to live inside ``state.blob``.

Their presence in a block is what says this file predates the tier that gave
each of them a table. Draining is keyed on them rather than on a version
number, so it is idempotent by construction: the keys are removed as they
are read, and a file that has none is already current.
"""


def _drain_blobs(conn: sqlite3.Connection) -> None:
    """Move the collections out of every state blob into their tables, once.

    The last shape change of this tier, and the one with a real loss to
    declare: an ``alive`` bucket carried no timestamp of its own (one shared
    heartbeat stamp decayed them all), so each is adopted as if last touched
    NOW. The curve keeps its shape and its relative weights; what it loses is
    that the whole thing should already have faded by however long the host
    was down before this upgrade. It re-earns that within a half-life.

    Everything else moves whole. ``log`` is DROPPED rather than moved: it was
    a capped second copy of acts ``contact`` already holds, and /status reads
    the sittings back out of that now.
    """
    blocks = {
        str(r['service']): json.loads(str(r['blob']))
        for r in conn.execute('SELECT service, blob FROM state')
    }
    stale = {
        name: block
        for name, block in blocks.items()
        if isinstance(block, dict) and any(key in block for key in _BLOB_KEYS)
    }
    if not stale:
        return
    now = time.time()
    with conn:
        for name, block in stale.items():
            _Drain(conn, name, now).run(block)
            conn.execute(
                'UPDATE state SET blob = ? WHERE service = ?',
                (json.dumps(block), name),
            )
    log.info('state: drained collections out of %s', ', '.join(sorted(stale)))


@dataclass
class _Drain:
    """One service's move out of the blob, table by table.

    A tiny bound object rather than four arguments repeated five times: the
    connection, whose service is being drained, and the moment we are doing
    it are the same for every table below.
    """

    conn: sqlite3.Connection
    service: str
    now: float

    def run(self, block: dict[str, object]) -> None:
        """Empty ``block``'s collections into the tables, mutating it."""
        self.uptime(block.pop('alive', None))
        self.queue(block.pop('queue', None))
        self.watching(block.pop('posts', None))
        self.emoji(block.pop('emoji_last', None))
        self.departures(block.pop('left', None))
        self.register('posted', block.pop('posted', None))
        self.register('pending', block.pop('pending', None))
        self.rejected(block.pop('rejected', None))
        self.roster(block)
        block.pop('log', None)  # a second copy of contact; contact wins
        block.pop('groups', None)  # the pending key's older name
        block.pop('processed_ids', None)  # rebuilt from posted.msg_ids now
        block.pop('alive_at', None)  # each bucket carries its own stamp now
        session = block.pop('session', None)
        if isinstance(session, dict):
            block.update({str(k): v for k, v in session.items()})

    def uptime(self, raw: object) -> None:
        """Adopt the learned uptime curve, one row per hour bucket."""
        for hour, weight in _floats(raw).items():
            self.conn.execute(
                'INSERT OR REPLACE INTO uptime (hour, weight, at) '
                'VALUES (?, ?, ?)',
                (int(hour), weight, self.now),
            )

    def queue(self, raw: object) -> None:
        """Adopt the scheduled reactions."""
        for row in _rows(raw):
            self.conn.execute(
                'INSERT OR REPLACE INTO scheduled '
                '(service, chat, reply_to, root, due_at, kind, text, emojis) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    self.service,
                    _as_int(row.get('chat')),
                    _as_int(row.get('reply_to')),
                    _as_int(row.get('root')),
                    _as_float(row.get('when')),
                    str(row.get('kind', 'react')),
                    str(row.get('text', '')),
                    json.dumps(row.get('emojis', [])),
                ),
            )

    def watching(self, raw: object) -> None:
        """Adopt the watch window of (chat, post) pairs."""
        for pair in _rows(raw, shape=list):
            self.conn.execute(
                'INSERT OR REPLACE INTO watching '
                '(service, chat_id, post_id, at) VALUES (?, ?, ?, ?)',
                (self.service, _as_int(pair[0]), _as_int(pair[1]), self.now),
            )

    def departures(self, raw: object) -> None:
        """Adopt the greeter's welcome-back memory as marks."""
        for uid in _rows(raw, shape=int):
            self.conn.execute(
                'INSERT OR IGNORE INTO marks (service, key, at) '
                'VALUES (?, ?, ?)',
                (self.service, f'left:{_as_int(uid)}', self.now),
            )

    def register(self, table: str, raw: object) -> None:
        """Adopt the poster's posted or pending register.

        ``at`` and ``since`` were ISO strings in the blob and are epochs in
        the table, so this is where that conversion happens -- once, on the
        way in, rather than on every comparison the re-post guard makes.
        """
        stamp = 'at' if table == 'posted' else 'since'
        for row in _rows(raw):
            self.conn.execute(
                f'INSERT INTO {table} '  # noqa: S608 -- a literal, twice above
                f'(title, {stamp}, {"links" if stamp == "at" else "items"}, '
                'msg_ids) VALUES (?, ?, ?, ?)',
                (
                    str(row.get('title', '')),
                    _epoch(row.get(stamp)),
                    json.dumps(
                        row.get('links' if stamp == 'at' else 'items') or {}
                    ),
                    json.dumps(row.get('msg_ids') or []),
                ),
            )

    def rejected(self, raw: object) -> None:
        """Adopt the poster's rejected titles, in order."""
        for title in _rows(raw, shape=str):
            self.conn.execute(
                'INSERT INTO rejected (title) VALUES (?)', (str(title),)
            )

    def roster(self, block: dict[str, object]) -> None:
        """Adopt the cabinet, whose whole block WAS the roster.

        No named key to pop: every entry mapping a nick to an object with an
        ``at`` was a resident, so the block empties itself here. Only the
        cabinet ever wrote a block shaped like that, which is what makes it
        safe to recognise by shape rather than by name.
        """
        for nick in [k for k, v in block.items() if _resident(v)]:
            entry = _table(block.pop(nick))
            self.conn.execute(
                'INSERT OR REPLACE INTO cabinet (nick, at, amount) '
                'VALUES (?, ?, ?)',
                (
                    nick,
                    _as_float(entry.get('at')),
                    str(entry.get('amount', '')),
                ),
            )

    def emoji(self, raw: object) -> None:
        """Adopt when each emoji was last used."""
        for emoji, at in _floats(raw).items():
            self.conn.execute(
                'INSERT OR REPLACE INTO emoji_used (service, emoji_id, at) '
                'VALUES (?, ?, ?)',
                (self.service, emoji, at),
            )


def _resident(value: object) -> bool:
    """Whether one blob entry is a cabinet resident (a nick -> {at, ...})."""
    return isinstance(value, dict) and 'at' in value and 'amount' in value


def _table(value: object) -> dict[str, object]:
    """Read one persisted value as an object; an empty one when it is not."""
    return dict(value) if isinstance(value, dict) else {}


def _epoch(value: object) -> float:
    """Read a persisted moment, whether it is a number or an ISO string.

    The poster wrote ISO into its blob and epochs go into its table, so this
    is the one place the two formats meet. An unreadable stamp reads as NOW,
    not as 0: the only consumer is the re-post guard, and a record that looks
    freshly posted BLOCKS a re-post while one that looks ancient lets a
    duplicate through. Damaged state should cost a missed post, not a double.
    """
    if _number(value):
        return float(value)
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return time.time()


def _as_int(value: object) -> int:
    """Read one persisted number as an int; 0 for anything else."""
    return int(value) if _number(value) else 0


def _as_float(value: object) -> float:
    """Read one persisted number as a float; 0.0 for anything else."""
    return float(value) if _number(value) else 0.0


def _floats(raw: object) -> dict[str, float]:
    """Read a persisted ``{key: number}`` block, skipping anything else."""
    if not isinstance(raw, dict):
        return {}
    return {str(k): float(v) for k, v in raw.items() if _number(v)}


def _number(value: object) -> TypeGuard[int | float]:
    """Whether a persisted value is a plain number.

    A TypeGuard, not a bool, so the readers above narrow: ``int(value)`` on
    an ``object`` is the kind of thing that only fails once a hand-edited
    JSON file reaches it. Bools are excluded on purpose -- ``True`` is an
    ``int`` in Python, and a chat id of 1 is not what a written ``true``
    meant.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _rows(raw: object, shape: type = dict) -> list:  # type: ignore[type-arg]
    """Read a persisted list, keeping only entries of the wanted shape.

    A pair-shaped entry (the watch window's ``[chat, post]``) additionally
    has to have both halves; a scalar entry is kept as it is.
    """
    if not isinstance(raw, list):
        return []
    if shape is list:
        return [
            r for r in raw if isinstance(r, (list, tuple)) and len(r) >= _PAIR
        ]
    return [r for r in raw if isinstance(r, shape)]


_PAIR = 2
"""A persisted (chat, post) row is two numbers; a queue row has more keys."""


def _fold_peers(conn: sqlite3.Connection) -> None:
    """Split the old ``peers`` table into ``standing`` plus ``actors``.

    Two things came out of one row. The counters go to ``standing`` with the
    id finally stored as a number. The ``label`` was a string this bot
    composed itself -- ``'@name (id)'`` or ``'"Title" (id)'`` -- so it is
    taken apart by the same rule that built it and stored as fields. Losing
    it instead would cost a Telegram resolution per peer to learn again
    what we already knew.
    """
    have = _own_columns(conn, 'peers')
    rows = conn.execute('SELECT * FROM peers').fetchall()
    conn.executemany(
        'INSERT OR IGNORE INTO standing '
        '(service, peer_id, offered, taken, recip, last_at, take_at, '
        'gap_n, gap_sum, gap_sq, burst) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        [
            (
                row['service'],
                int(row['peer_id']),
                *(row[name] if name in have else 0 for name in _FOLDED),
            )
            for row in rows
            if str(row['peer_id']).lstrip('-').isdigit()
        ],
    )
    for row in rows:
        actor = _from_label(str(row['peer_id']), str(row['label'] or ''))
        if actor is not None:
            conn.execute(
                'INSERT OR IGNORE INTO actors '
                '(peer_id, kind, username, title) VALUES (?, ?, ?, ?)',
                (actor.peer_id, actor.kind, actor.username, actor.title),
            )


def _fold_users(conn: sqlite3.Connection) -> None:
    """Split the old ``users`` table into ``actors`` plus ``audience``.

    Identity and membership were one row keyed by ``user_id``; they are one
    actor and one aggregate over them now. ``kind`` is 'user' for every one
    of these -- the audience table only ever held people.
    """
    conn.execute(
        'INSERT OR IGNORE INTO actors (peer_id, kind, username, first_name, '
        'last_name, phone, first_seen, last_seen, updated_at) '
        "SELECT user_id, 'user', coalesce(username, ''), "
        "coalesce(first_name, ''), coalesce(last_name, ''), "
        "coalesce(phone, ''), coalesce(first_seen, 0), "
        'coalesce(last_seen, 0), coalesce(updated_at, 0) FROM users'
    )
    conn.execute(
        'INSERT OR IGNORE INTO audience (peer_id, subscribed, msg_count) '
        'SELECT user_id, subscribed, msg_count FROM users'
    )
    for table in ('membership_events', 'messages'):
        if 'user_id' in _own_columns(conn, table):
            conn.execute(
                f'ALTER TABLE {table} RENAME COLUMN user_id TO peer_id'
            )


_FOLDED = (
    'offered',
    'taken',
    'recip',
    'last_at',
    'take_at',
    'gap_n',
    'gap_sum',
    'gap_sq',
    'burst',
)
"""What a ``peers`` row carried besides its key and its label.

Read by name with a default, because the oldest files on record stop after
``last_at`` -- the timing columns were added later, and a fold that assumed
them would fail on exactly the installs that most need migrating.
"""


def _own_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the columns of a table in THIS database (not an attached one)."""
    return {
        str(row['name']) for row in conn.execute(f'PRAGMA table_info({table})')
    }


def _from_label(peer_id: str, label: str) -> Actor | None:
    """Take a composed ``'@name (id)'`` label back apart into an Actor.

    ``None`` when there is nothing to learn: a blank label, or one that is
    just the bare id because the peer was never resolved.
    """
    if not peer_id.lstrip('-').isdigit():
        return None
    name = label.removesuffix(f' ({peer_id})').strip()
    kind = 'chat' if peer_id.startswith('-') else 'user'
    if not name or name == peer_id:
        return Actor(int(peer_id), kind)
    if name.startswith('@'):
        return Actor(int(peer_id), kind, username=name[1:])
    return Actor(int(peer_id), kind, title=name.strip('"'))


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

    # --- who: one identity, shared by every service ----------------------

    def actor(self, peer_id: int) -> Actor:
        """Return what we know about a peer, or a bare Actor if nothing."""
        got = self.conn.execute(
            'SELECT * FROM actors WHERE peer_id = ?', (peer_id,)
        ).fetchone()
        return _actor(got) if got is not None else Actor(peer_id)

    def actors(self, peer_ids: Iterable[int]) -> dict[int, Actor]:
        """Return what we know about several peers, keyed by id.

        Missing ones are simply absent, so a caller can tell "we have never
        resolved this peer" from "this peer has no name", which is the
        difference between asking Telegram again and not bothering.
        """
        wanted = list(dict.fromkeys(peer_ids))
        if not wanted:
            return {}
        marks = ', '.join('?' * len(wanted))
        rows = self.conn.execute(
            f'SELECT * FROM actors WHERE peer_id IN ({marks})',  # noqa: S608 -- placeholders, not values
            wanted,
        )
        return {int(r['peer_id']): _actor(r) for r in rows}

    def note_actor(self, actor: Actor, at: float | None = None) -> None:
        """Record who a peer is; a blank field keeps what we already knew.

        Telegram answers a resolution with whatever it feels like sharing --
        ``username`` for one call, only ``first_name`` for the next -- so a
        later, thinner answer must not erase a fuller one. Hence the
        blank-keeps-the-old rule rather than a plain overwrite.
        """
        now = time.time() if at is None else at
        known = [getattr(actor, name) for name in IDENTITY]
        self.conn.execute(ACTOR_SQL, (actor.peer_id, *known, now, now, now))
        self.conn.commit()

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

    def peer(self, peer_id: int) -> PeerRow:
        """Return one peer's row, or an empty row if it has none yet."""
        got = self.conn.execute(
            'SELECT * FROM standing WHERE service = ? AND peer_id = ?',
            (self.service, peer_id),
        ).fetchone()
        return _row(got) if got is not None else PeerRow(peer_id)

    def met(self, peer_id: int) -> float:
        """Return when we first did ANYTHING with a person; 0 if never.

        Deliberately not bound to this view's service, and the one method
        here that is not. An arc belongs to a PERSON, not to a service: one
        account is one person, so the day we met somebody is the same day
        whether the story engine noticed them first or the like engine did,
        and two clocks would put the same person in the honeymoon over here
        and the cold shoulder over there.

        Read from ``contact`` rather than ``actors.first_seen``, because a
        peer we have acted on always has a row here, while ``first_seen`` is
        only set once somebody resolves their name. Zero means no history at
        all, which is not a missing value -- it is a person we are meeting
        right now, and the arc starts them at its beginning.
        """
        got = self.conn.execute(
            'SELECT min(at) AS first FROM contact WHERE peer_id = ?',
            (peer_id,),
        ).fetchone()
        return float(got['first']) if got['first'] is not None else 0.0

    def peers(self, limit: int = 0) -> list[PeerRow]:
        """Return this service's peers, most recently engaged first."""
        sql = 'SELECT * FROM standing WHERE service = ? ORDER BY last_at DESC'
        args: tuple[object, ...] = (self.service,)
        if limit > 0:
            sql, args = sql + ' LIMIT ?', (self.service, limit)
        return [_row(r) for r in self.conn.execute(sql, args)]

    def bump(  # noqa: PLR0913 -- peer + counts + moment + what it was about
        self,
        peer_id: int,
        counts: Mapping[str, float],
        at: float | None = None,
        subjects: tuple[int, ...] = (),
    ) -> None:
        """Record what happened: the log rows AND the running totals.

        Both, in one call and one transaction, because they are one fact
        told twice. ``contact`` gets a row per subject -- what we did and
        which story or comment we did it about -- and ``standing`` gets the
        same acts added to its counters. Written apart, the two would be free
        to disagree, and the counter is what the model reads.

        The row says the HIGHEST rung this bump reached: a bump that counts
        an offer and a take is one story seen, not one ignored and one seen.
        Which is why ``_reached`` reads ``RUNGS`` backwards.

        ``counts`` names the columns to add to (any of ``COUNTERS``); absent
        ones are left alone. The gap statistics are floats, the rest whole
        numbers, and SQLite stores each by its column's affinity.

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

        ``subjects`` names the things acted upon, one per counted unit. Fewer
        (or none) is allowed and still logs the right NUMBER of rows, with 0
        for the ones nobody named -- the act happened either way, and a log
        that dropped it would stop matching the counter beside it.
        """
        adds = [counts.get(name, 0) for name in COUNTERS]
        moment = time.time() if at is None else at
        took = moment if counts.get('taken') else 0.0
        # A person exists the moment we act on them, whether or not anybody
        # has resolved their name yet. Without this the foreign key would
        # refuse the write -- and rightly: a standing row for somebody with
        # no row in ``actors`` is a relationship with an id, which is what
        # /who used to print when it could not find them.
        self.conn.execute(
            'INSERT OR IGNORE INTO actors (peer_id) VALUES (?)', (peer_id,)
        )
        self.conn.execute(
            _BUMP_SQL, (self.service, peer_id, *adds, moment, took)
        )
        act, done = self._reached(counts)
        self.conn.executemany(
            'INSERT INTO contact (peer_id, at, service, act, subject) '
            'VALUES (?, ?, ?, ?, ?)',
            [
                (peer_id, moment, self.service, act, subject)
                for subject in _each(done, subjects)
            ],
        )
        self.conn.commit()

    def _reached(self, counts: Mapping[str, float]) -> tuple[str, int]:
        """Return the act this bump amounts to, in this service's words.

        Highest rung wins, so ``{'offered': 2, 'taken': 2}`` is two stories
        SEEN rather than two ignored and two seen. Nothing counted is not an
        error -- the timing statistics bump on their own -- and logs nothing.
        """
        ladder = ACTS[self.service]
        for rung in reversed(range(len(RUNGS))):
            done = int(counts.get(RUNGS[rung], 0))
            if done:
                return ladder[rung], done
        return '', 0

    # --- the relationship history ----------------------------------------

    def history(self, peer_id: int, limit: int = 0) -> list[Contact]:
        """Return everything we ever did with a peer, most recent first.

        The question this whole table exists to answer, in one query. It
        used to take four: the counters here, the dedup keys in ``marks``
        under a stringly-typed key, a rolling log inside a JSON blob, and
        the audience tables -- none of which could be joined to the others.
        """
        sql = (
            'SELECT peer_id, at, act, subject FROM contact '
            'WHERE peer_id = ? AND service = ? ORDER BY at DESC, id DESC'
        )
        args: tuple[object, ...] = (peer_id, self.service)
        if limit > 0:
            sql, args = sql + ' LIMIT ?', (peer_id, self.service, limit)
        return [
            Contact(
                peer_id=int(r['peer_id']),
                at=float(r['at']),
                act=str(r['act']),
                subject=int(r['subject']),
            )
            for r in self.conn.execute(sql, args)
        ]

    def acts(self, peer_id: int) -> dict[str, int]:
        """Count a peer's logged acts by kind, in this service's words."""
        rows = self.conn.execute(
            'SELECT act, count(*) AS n FROM contact '
            'WHERE peer_id = ? AND service = ? GROUP BY act',
            (peer_id, self.service),
        )
        return {str(r['act']): int(r['n']) for r in rows}

    def acts_since(self, act: str, at: float) -> list[float]:
        """Return when each ``act`` happened since ``at``, oldest first.

        For "how many today", which the odometer in the blob could not
        answer: it counted forever, and the rolling log that could answer it
        was capped, so a busy day read short.
        """
        return [
            float(r['at'])
            for r in self.conn.execute(
                'SELECT at FROM contact '
                'WHERE service = ? AND act = ? AND at >= ? ORDER BY at',
                (self.service, act, at),
            )
        ]

    def glances(self, act: str, gap: float, limit: int) -> list[sqlite3.Row]:
        """Group a service's acts into SITTINGS, most recent first.

        Acts of one person closer together than ``gap`` are one visit, which
        is what a reader of /status means by "watched 5 of her stories" --
        five rows in the log, one line in the readout.

        This is why the rolling view log inside the JSON blob is gone rather
        than moved: it was a SECOND recording of acts ``contact`` already
        holds, capped at 50 and therefore quietly disagreeing with the
        counters as soon as the 51st landed. Derived, it cannot disagree,
        and it is no longer capped at all.

        Gaps-and-islands: a row more than ``gap`` after the previous one of
        the same person opens a new run, the running sum of those numbers
        the runs, and the group-by counts them. ``gap`` is the same
        ``burst_gap_sec`` that decides massed attention for the model, so
        "one sitting" means one thing in this file.
        """
        return list(
            self.conn.execute(
                'SELECT peer_id, count(*) AS n, max(at) AS ts,'
                '       max(id) AS last FROM ('
                '  SELECT peer_id, id, at, sum(fresh) OVER'
                '    (PARTITION BY peer_id ORDER BY at, id) AS run FROM ('
                '    SELECT peer_id, id, at, CASE WHEN at - lag(at) OVER'
                '      (PARTITION BY peer_id ORDER BY at, id) <= ?'
                '      THEN 0 ELSE 1 END AS fresh'
                '    FROM contact WHERE service = ? AND act = ?))'
                ' GROUP BY peer_id, run ORDER BY ts DESC, last DESC LIMIT ?',
                (gap, self.service, act, limit),
            )
        )

    def tally(self, peer_id: int) -> dict[str, int]:
        """Return the counters as the LOG says they should be.

        The other half of the invariant: ``peer()`` reads what we recorded,
        this recomputes it from what happened, and a test (and /who) assert
        the two agree. That is the whole reason the log is written in the
        same transaction as the totals.

        ``offered`` is every chance -- the ones we passed on plus the ones we
        took -- because a chance we took was never separately offered. The
        top rung is counted on its own, since a reciprocation is a second
        act on a subject the middle rung already counted.
        """
        seen = self.acts(peer_id)
        passed, took, back = ACTS[self.service]
        return {
            'offered': seen.get(passed, 0) + seen.get(took, 0),
            'taken': seen.get(took, 0),
            'recip': seen.get(back, 0),
        }

    def forget(self, peer_id: int) -> None:
        """Drop a peer's standing WITH THIS SERVICE (it rolled off the set).

        Their row in ``actors`` stays: who they are is not this service's to
        forget, and the other service may still be talking to them.
        """
        self.conn.execute(
            'DELETE FROM standing WHERE service = ? AND peer_id = ?',
            (self.service, peer_id),
        )
        self.conn.commit()

    def trim_peers(self, keep: int) -> list[int]:
        """Drop all but the ``keep`` most recent peers; return what went.

        The tracked set is bounded so a long-running account does not carry
        every peer it ever met. The caller clears whatever else it keyed by
        those peers -- the store does not know their mark format.
        """
        if keep <= 0:
            return []
        rows = self.conn.execute(
            'SELECT peer_id FROM standing WHERE service = ? '
            'ORDER BY last_at DESC LIMIT -1 OFFSET ?',
            (self.service, keep),
        ).fetchall()
        dropped = [int(r['peer_id']) for r in rows]
        for peer_id in dropped:
            self.forget(peer_id)
        return dropped

    # --- the host's learned uptime ---------------------------------------

    def note_hour(self, hour: int, half_life: float, at: float) -> None:
        """Heartbeat: credit one observation to ``hour``, decaying what is.

        ONE row, whatever else the service holds. This is the write that
        motivated the whole tier: it runs every sixty seconds, and while it
        lived in the JSON blob each ``+1`` rewrote the entire block -- about
        15 MB a day to store a number that fits in eight bytes.

        The decay is per row, from that row's OWN last touch, not from one
        shared heartbeat stamp. Exponential decay composes, so decaying a
        bucket by the time since IT was touched gives exactly what decaying
        every bucket on every beat gave -- and it costs one row instead of
        twenty-four plus a scalar to remember when they were last swept.
        """
        self.conn.execute(
            'INSERT INTO uptime (hour, weight, at) VALUES (?, ?, ?) '
            'ON CONFLICT (hour) DO UPDATE SET '
            'weight = weight * '
            'pow(0.5, (excluded.at - uptime.at) / ?) + excluded.weight, '
            'at = excluded.at',
            (hour, _ALIVE_STEP, at, half_life or _FOREVER),
        )
        self.conn.commit()

    def hours(self, half_life: float, at: float) -> dict[int, float]:
        """Return the learned uptime curve, every bucket decayed to ``at``.

        Decaying on READ is what lets the write above touch one row: a
        bucket nobody has visited in a month is worth less than one visited
        yesterday, and that is a property of when it was last written, not
        of how often the sweep ran.
        """
        return {
            int(r['hour']): float(r['weight'])
            * 0.5 ** ((at - float(r['at'])) / (half_life or _FOREVER))
            for r in self.conn.execute('SELECT hour, weight, at FROM uptime')
        }

    # --- the queue, the watch window, the emoji recency ------------------

    def enqueue(self, rows: Iterable[tuple[object, ...]]) -> None:
        """Replace this service's scheduled work with ``rows``.

        Still a whole-collection write, and deliberately so: the queue is
        re-planned as a set (a /requeue re-arms every timer), so writing it
        as one is what the caller actually means. What changed is that it no
        longer drags the mood, the counters and the uptime curve along with
        it -- a queue write now costs the queue.
        """
        self.conn.execute(
            'DELETE FROM scheduled WHERE service = ?', (self.service,)
        )
        self.conn.executemany(
            'INSERT OR REPLACE INTO scheduled '
            '(service, chat, reply_to, root, due_at, kind, text, emojis) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            [(self.service, *row) for row in rows],
        )
        self.conn.commit()

    def queued(self) -> list[sqlite3.Row]:
        """Return this service's scheduled work, soonest first."""
        return list(
            self.conn.execute(
                'SELECT chat, reply_to, root, due_at, kind, text, emojis '
                'FROM scheduled WHERE service = ? ORDER BY due_at',
                (self.service,),
            )
        )

    def watch(self, posts: Iterable[tuple[int, int]], at: float) -> None:
        """Set the posts this service is watching for comments."""
        self.conn.execute(
            'DELETE FROM watching WHERE service = ?', (self.service,)
        )
        self.conn.executemany(
            'INSERT OR REPLACE INTO watching '
            '(service, chat_id, post_id, at) VALUES (?, ?, ?, ?)',
            [(self.service, chat, post, at) for chat, post in posts],
        )
        self.conn.commit()

    def watched(self) -> list[tuple[int, int]]:
        """Return the (chat, post) pairs inside the watch window."""
        return [
            (int(r['chat_id']), int(r['post_id']))
            for r in self.conn.execute(
                'SELECT chat_id, post_id FROM watching '
                'WHERE service = ? ORDER BY at, post_id',
                (self.service,),
            )
        ]

    def note_emoji(self, emoji_id: str, at: float) -> None:
        """Stamp when an emoji was last used (one row, for the recency law)."""
        self.conn.execute(
            'INSERT INTO emoji_used (service, emoji_id, at) VALUES (?, ?, ?) '
            'ON CONFLICT (service, emoji_id) DO UPDATE SET at = excluded.at',
            (self.service, emoji_id, at),
        )
        self.conn.commit()

    def emoji_seen(self) -> dict[str, float]:
        """Return when each emoji was last used, for the recency penalty."""
        return {
            str(r['emoji_id']): float(r['at'])
            for r in self.conn.execute(
                'SELECT emoji_id, at FROM emoji_used WHERE service = ?',
                (self.service,),
            )
        }

    # --- the poster's registers and the cabinet --------------------------
    # No service column on these four, deliberately: each has exactly ONE
    # owner, the way ``actors`` and ``audience`` belong to the profile rather
    # than to a service. A column saying 'aggregator' on every row of a table
    # only the poster ever touches would be a fact about nothing.

    def set_rows(self, table: str, rows: Iterable[tuple[object, ...]]) -> None:
        """Replace one single-owner register with ``rows``.

        The poster keeps its registers as capped, ordered lists it rewrites
        as a whole (a post is appended and the oldest falls off), so writing
        them whole is what the caller means. What it no longer means is
        rewriting the pending groups and the rejected titles too, which is
        what one shared JSON block made every save do.
        """
        self.conn.execute(f'DELETE FROM {table}')  # noqa: S608
        marks = ', '.join('?' * len(_REGISTERS[table]))
        self.conn.executemany(
            f'INSERT INTO {table} ({", ".join(_REGISTERS[table])}) '  # noqa: S608
            f'VALUES ({marks})',
            rows,
        )
        self.conn.commit()

    def rows_of(self, table: str) -> list[sqlite3.Row]:
        """Return one single-owner register in insertion order."""
        if table not in _REGISTERS:
            msg = f'not a register: {table}'
            raise KeyError(msg)
        return list(
            self.conn.execute(
                f'SELECT {", ".join(_REGISTERS[table])} FROM {table} '  # noqa: S608
                'ORDER BY id'
            )
        )

    def shelve(self, nick: str, at: float, amount: str) -> None:
        """Move one supporter onto a cabinet shelf (or refresh their timer)."""
        self.conn.execute(
            'INSERT INTO cabinet (nick, at, amount) VALUES (?, ?, ?) '
            'ON CONFLICT (nick) DO UPDATE SET '
            'at = excluded.at, amount = excluded.amount',
            (nick, at, amount),
        )
        self.conn.commit()

    def evict(self, nick: str) -> bool:
        """Remove one resident by hand; True if they were there."""
        cur = self.conn.execute('DELETE FROM cabinet WHERE nick = ?', (nick,))
        self.conn.commit()
        return cur.rowcount > 0

    def residents(self, since: float) -> list[sqlite3.Row]:
        """Return everyone who moved in at or after ``since``, newest first.

        Expiry is a WHERE clause now rather than a rebuild-and-rewrite on
        every read, so nobody is pruned by the act of looking.
        """
        return list(
            self.conn.execute(
                'SELECT nick, at, amount FROM cabinet '
                'WHERE at >= ? ORDER BY at DESC',
                (since,),
            )
        )

    def sweep_cabinet(self, since: float) -> None:
        """Drop residents whose month ran out (called when one moves in)."""
        self.conn.execute('DELETE FROM cabinet WHERE at < ?', (since,))
        self.conn.commit()

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

        Missing and unreadable both mean "start from your defaults", which
        is safe for everything still kept here: a mood, a cursor, a daily
        counter, each cheaply re-earned.

        There used to be a ``read_strict`` beside this, for the one caller
        whose empty result would have been a LIE -- the poster reading
        "nothing was ever posted", disarming its re-post guard and
        republishing the backlog. Its log is ``posted`` now, and rows do not
        half-parse: they are there or the database is, so the failure it
        guarded against cannot be reached from here any more.
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


def _each(count: int, subjects: tuple[int, ...]) -> list[int]:
    """Return one subject per counted unit, padding with 0 when short.

    The COUNT is what has to match, because it is what the model reads off
    ``standing``. Naming the things is better and usually possible, but a
    caller that cannot name them still gets the right number of rows.
    """
    if count <= 0:
        return []
    named = list(subjects[:count])
    return named + [0] * (count - len(named))


def _actor(row: sqlite3.Row) -> Actor:
    """Build an Actor from one database row."""
    return Actor(
        peer_id=int(row['peer_id']),
        **{name: str(row[name]) for name in IDENTITY},
    )


def _row(row: sqlite3.Row) -> PeerRow:
    """Build a PeerRow from one database row."""
    return PeerRow(
        peer_id=int(row['peer_id']),
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

MARK_COLUMNS = ('key', 'at')
AUDIENCE_TABLES = ('membership_events', 'messages')
LEGACY_PEER = ('peer_id', 'label', *_FOLDED)
"""What a row is worth carrying over, named once so the import cannot drift.

``LEGACY_PEER`` is the shape a ``peers`` row had, not the shape ``standing``
has now, and that is the point: this import moves ROWS BETWEEN FILES without
changing them, and ``_migrate`` changes SHAPE within one file. Two jobs, two
places, one conversion path -- an import that also reshaped would be a second
copy of the fold, free to disagree with the first.
"""

_STAGING = """
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
"""
"""The two tables an older file's rows land in before ``_migrate`` folds them.

Created only when there is something to put in them, and dropped by the fold,
so a fresh install never sees either. Every legacy ledger shape normalises
into this one -- the shared file's ``engine`` column and the per-service
file's filename both become ``service`` -- which is what leaves the fold with
a single input to understand.
"""


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
    # Off for the import, like the fold inside ``connect``, and for the same
    # reason: rows arrive table by table, so a ledger row can land before the
    # actor it names. ``_adopt_orphans`` closes that gap once every file is
    # in, and the pragma goes back on before anything live writes. SQLite
    # ignores this inside a transaction, which is why it is set out here.
    # This connection is closed at the end of the import; every later open
    # turns them back on, so nothing live ever writes without them.
    conn.execute('PRAGMA foreign_keys = OFF')
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
        _migrate(conn)  # fold what the import staged, in this same open
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

    Peer rows land in the staging ``peers`` table, in the shape they had.
    ``_migrate`` turns them into ``standing`` plus ``actors`` afterwards --
    the same fold a directly upgraded install goes through.
    """
    took = False
    for table, wanted in (('peers', LEGACY_PEER), ('marks', MARK_COLUMNS)):
        have = _columns(conn, table)
        names = ', '.join(name for name in wanted if name in have)
        if not names:
            continue
        conn.executescript(_STAGING)
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

    The events and messages come straight across, with the old ``user_id``
    read into today's ``peer_id`` -- one person, one column name, everywhere.
    The identity rows go to the staging ``users`` table for ``_migrate``,
    which is what splits them into an actor and their membership.
    """
    took = False
    for table in AUDIENCE_TABLES:
        have = _columns(conn, table)
        if not have:
            continue
        source = ', '.join(sorted(have))
        target = source.replace('user_id', 'peer_id')
        conn.execute(
            f'INSERT OR IGNORE INTO {table} ({target}) '  # noqa: S608 -- schema names, not input
            f'SELECT {source} FROM old.{table}'
        )
        took = True
    if _columns(conn, 'users'):
        conn.executescript(_STAGING)
        conn.execute('INSERT OR IGNORE INTO users SELECT * FROM old.users')
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
