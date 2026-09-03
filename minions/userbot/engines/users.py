# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Persistent users database for the aggregator (opt-in, SQLite).

Records the channel audience over time: the subscribe/unsubscribe timeline per
person, their identity (username, id, name, phone), and every message the
account can see, with text. This is strictly a DATA layer -- Telethon-free, so
it unit-tests against a temp database; ``main.py`` feeds it from the greeter's
admin-log stream (membership) and the message handler (messages).

The audience is NOT a service: it has no ledger, no cursor and no name to
key rows by -- one channel's members belong to the profile. So its three
tables sit in the profile's one state database beside every service's, and
this module holds the queries against them while ``core/state.py`` holds
their DDL along with the rest of that file's schema.

Two facts shape the schema:

* Phone is almost never available -- Telegram exposes ``User.phone`` only to
  mutual contacts, so that column is null for virtually every member.
* Only messages the account can SEE are captured (the linked discussion group's
  comments and the source chat), never DMs or plain channel posts.

Every write is idempotent (``INSERT OR IGNORE`` on a natural key), so the
greeter re-reading deferred admin-log events, and comment rescans, never
double-count. All UI text lives in the constants JSON, so this source is ASCII.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from minions.userbot.core import state

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable


@dataclass(frozen=True)
class MembershipEvent:
    """One join/leave event: who, which way, and the admin-log cursor."""

    user_id: int
    joined: bool
    left: bool
    admin_log_id: int | None = None
    ts: float | None = None


@dataclass(frozen=True)
class SeenMessage:
    """One observed message: author, where it landed, and its body."""

    user_id: int
    chat_id: int
    msg_id: int
    root: int = 0
    text: str = ''
    ts: float | None = None


@dataclass(frozen=True)
class Identity:
    """A user's identity fields; a ``None`` field keeps the stored value."""

    user_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None


class UserStore:
    """A per-profile SQLite store of the channel audience and its activity.

    ``path`` is the database file (``users.db`` under the profile's state dir);
    ``':memory:'`` is accepted for tests. ``clock`` is injected so tests can
    pin timestamps, exactly like ``ReactionBrain``.
    """

    clock: Callable[[], float]

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Bind the profile's state database; the tables are already there.

        The connection is the bot's, not this object's: the audience lives
        in the same file as every service's state, so there is nothing here
        to open and nothing to close. Its tables are declared with the rest
        of that file's schema in ``core/state.py``.
        """
        self._conn = conn
        self.clock = time.time

    def _ensure_user(self, peer_id: int, now: float) -> None:
        """Make sure this id has an actor row and a membership row.

        Two rows because they answer two questions: ``actors`` is who they
        are, which every service shares, and ``audience`` is their standing
        with OUR channel, which only this module keeps.
        """
        self._conn.execute(
            'INSERT OR IGNORE INTO actors (peer_id, kind, first_seen, '
            "last_seen, updated_at) VALUES (?, 'user', ?, ?, ?)",
            (peer_id, now, now, now),
        )
        self._conn.execute(
            'INSERT OR IGNORE INTO audience (peer_id) VALUES (?)', (peer_id,)
        )

    def record_membership(self, event: MembershipEvent) -> bool:
        """Append one join/leave event and update the user's current state.

        Idempotent on ``admin_log_id`` (the greeter re-reads events it deferred
        past a DM cap), so a repeat is a no-op. Returns whether a NEW event was
        recorded.
        """
        if event.user_id <= 0 or not (event.joined or event.left):
            return False
        now = self.clock() if event.ts is None else event.ts
        kind = 'join' if event.joined else 'leave'
        cur = self._conn.execute(
            'INSERT OR IGNORE INTO membership_events '
            '(peer_id, event, ts, admin_log_id) VALUES (?, ?, ?, ?)',
            (event.user_id, kind, now, event.admin_log_id),
        )
        if cur.rowcount == 0:
            return False  # already recorded under this admin_log_id
        self._ensure_user(event.user_id, now)
        self._conn.execute(
            'UPDATE audience SET subscribed=? WHERE peer_id=?',
            (1 if event.joined else 0, event.user_id),
        )
        self._seen(event.user_id, now)
        self._conn.commit()
        return True

    def record_message(self, msg: SeenMessage) -> bool:
        """Store one seen message and bump the user's count/last_seen.

        Idempotent on ``(chat_id, msg_id)`` so a rescan of existing comments
        never double-counts. Returns whether a NEW message was stored.
        """
        if msg.user_id <= 0 or msg.msg_id <= 0:
            return False
        now = self.clock() if msg.ts is None else msg.ts
        cur = self._conn.execute(
            'INSERT OR IGNORE INTO messages '
            '(peer_id, chat_id, msg_id, root, text, ts) VALUES (?,?,?,?,?,?)',
            (msg.user_id, msg.chat_id, msg.msg_id, msg.root, msg.text, now),
        )
        if cur.rowcount == 0:
            return False  # already stored this message
        self._ensure_user(msg.user_id, now)
        self._conn.execute(
            'UPDATE audience SET msg_count = msg_count + 1 WHERE peer_id = ?',
            (msg.user_id,),
        )
        self._seen(msg.user_id, now)
        self._conn.commit()
        return True

    def _seen(self, peer_id: int, now: float) -> None:
        """Stamp when we last saw this peer do anything."""
        self._conn.execute(
            'UPDATE actors SET last_seen = ?, updated_at = ? '
            'WHERE peer_id = ?',
            (now, now, peer_id),
        )

    def apply_identity(self, identity: Identity) -> None:
        """Fill/refresh a user's identity; a ``None`` field keeps the old one.

        Called lazily from ``get_entity`` enrichment -- phone is almost always
        ``None`` (Telegram only exposes it to mutual contacts).
        """
        if identity.user_id <= 0:
            return
        now = self.clock()
        self._ensure_user(identity.user_id, now)
        self._conn.execute(
            state.ACTOR_SQL,
            (
                identity.user_id,
                'user',
                identity.username or '',
                '',  # a person has no channel title
                identity.first_name or '',
                identity.last_name or '',
                identity.phone or '',
                now,
                now,
                now,
            ),
        )
        self._conn.commit()

    def has_identity(self, user_id: int) -> bool:
        """Whether this user's username/name is known (skip re-enrich)."""
        row = self._conn.execute(
            'SELECT username, first_name FROM actors WHERE peer_id=?',
            (user_id,),
        ).fetchone()
        return bool(row and (row['username'] or row['first_name']))

    def summary(self) -> dict[str, int]:
        """Totals for /status and /users: users, subscribed now, left, msgs."""
        row = self._conn.execute(
            'SELECT COUNT(*) AS total, '
            'COALESCE(SUM(subscribed), 0) AS subscribed, '
            'COALESCE(SUM(msg_count), 0) AS messages FROM audience'
        ).fetchone()
        total = int(row['total'])
        subscribed = int(row['subscribed'])
        messages = int(row['messages'])
        return {
            'total': total,
            'subscribed': subscribed,
            'left': total - subscribed,
            'messages': messages,
        }

    def top_commenters(self, limit: int = 5) -> list[dict[str, object]]:
        """Return the most active commenters (by stored message count)."""
        rows = self._conn.execute(
            'SELECT a.peer_id AS user_id, a.username, a.first_name, '
            'n.msg_count FROM audience n '
            'JOIN actors a ON a.peer_id = n.peer_id '
            'WHERE n.msg_count > 0 '
            'ORDER BY n.msg_count DESC, a.peer_id LIMIT ?',
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_events(self, limit: int = 5) -> list[dict[str, object]]:
        """Return the latest join/leave events, newest first, with name."""
        rows = self._conn.execute(
            'SELECT e.peer_id AS user_id, e.event, e.ts, a.username, '
            'a.first_name FROM membership_events e '
            'LEFT JOIN actors a ON a.peer_id = e.peer_id '
            'ORDER BY e.id DESC LIMIT ?',
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def history(self, user_id: int) -> dict[str, object]:
        """One user's full record: their row plus their ordered event log."""
        user = self._conn.execute(
            'SELECT a.*, n.subscribed, n.msg_count FROM actors a '
            'LEFT JOIN audience n ON n.peer_id = a.peer_id '
            'WHERE a.peer_id = ?',
            (user_id,),
        ).fetchone()
        events = self._conn.execute(
            'SELECT event, ts FROM membership_events WHERE peer_id=? '
            'ORDER BY id',
            (user_id,),
        ).fetchall()
        return {
            'user': dict(user) if user else None,
            'events': [dict(e) for e in events],
        }
