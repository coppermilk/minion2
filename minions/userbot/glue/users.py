# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The opt-in audience log: who the account sees, and when they came and went.

A collaborator, not a mixin: the host builds one per profile and calls it.
Everything it needs arrives in ``AudienceDeps``, so what it can reach is
its constructor signature rather than whatever happens to be on ``self``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from minion_core.adapters import userchat
from minions.userbot.core import tasks
from minions.userbot.core.models import iso
from minions.userbot.engines import users

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable


log = logging.getLogger('userbot')

# How many rows each /users section lists.
REPORT_ROWS = 5

# Strangers queued for an identity lookup before the oldest is dropped.
# The queue exists to space the lookups out, not to guarantee every one:
# a backlog this long already means the account is seeing more new people
# than it can politely ask about, and the recent ones matter more.
ENRICH_BACKLOG = 500


def user_label(row: dict[str, object]) -> str:
    """Return a readable handle for a users-DB row: @username/name/id."""
    username = row.get('username')
    if username:
        return f'@{username}'
    name = row.get('first_name')
    if name:
        return str(name)
    return f'id {row.get("user_id", "?")}'


@dataclass(frozen=True)
class AudienceDeps:
    """Everything the audience log may reach; nothing else is in scope.

    ``watched`` answers which discussion chats the reaction engine is
    currently watching -- the audience log records messages there as well as
    in the source chat, but it does not own that list and must not cache it
    across a mode switch.
    """

    account: userchat.Account
    source: int
    store: users.UserStore
    watched: Callable[[], set[int]]
    enabled: bool = False
    store_text: bool = True
    enrich: bool = True


@dataclass
class AudienceLog:
    """Record the channel audience over time (off by default; it holds PII)."""

    deps: AudienceDeps
    # Strangers waiting for an identity lookup, and the single worker
    # that drains them (see _maybe_enrich).
    _waiting: dict[int, None] = field(default_factory=dict)
    _lookups: set[asyncio.Task[None]] = field(default_factory=set)

    def record_message(self, msg: userchat.Msg) -> None:
        """Log a seen audience message (a source or discussion comment).

        Records non-own messages in the source chat or a watched discussion
        group -- the chats the account actually sees -- bumping the sender's
        count and storing the text unless ``store_text`` is off, then
        enriching the sender's identity lazily. Idempotent per (chat, msg_id).
        """
        if not self.deps.enabled or msg.out or msg.sender_id <= 0:
            return
        if msg.chat_id != self.deps.source and (
            msg.chat_id not in self.deps.watched()
        ):
            return
        self.deps.store.record_message(
            users.SeenMessage(
                msg.sender_id,
                msg.chat_id,
                msg.id,
                root=msg.root,
                text=msg.text if self.deps.store_text else '',
            )
        )
        self._maybe_enrich(msg.sender_id)

    def note_membership(self, event: userchat.MemberEvent) -> None:
        """Greeter sink: persist a join/leave (idempotent on admin_log_id)."""
        if not self.deps.enabled or event.user_id <= 0:
            return
        self.deps.store.record_membership(
            users.MembershipEvent(
                event.user_id,
                joined=event.joined,
                left=event.left,
                admin_log_id=event.id,
            )
        )
        self._maybe_enrich(event.user_id)

    def waiting(self) -> int:
        """Return how many strangers are queued for an identity lookup."""
        return len(self._waiting)

    def close(self) -> None:
        """Drop in-flight lookups and release the SQLite handle."""
        tasks.cancel_all(self._lookups)
        self.deps.store.close()

    def _maybe_enrich(self, user_id: int) -> None:
        """Queue a one-off identity lookup for a user we do not know yet.

        A QUEUE with one worker, not a task per stranger. Every unknown
        person the account sees needs one lookup, and a busy chat produces
        them in bursts -- a task each meant an unbounded pile of coroutines
        all wanting the same connection at the same moment, which is what a
        flood wait is made of.

        Recency wins at both ends: the worker takes the most recently seen
        stranger first, and an overlong backlog drops its oldest. Someone
        queued a thousand messages ago is not who /users is about.
        """
        if (
            not self.deps.enrich
            or user_id <= 0
            or user_id in self._waiting
            or self.deps.store.has_identity(user_id)
        ):
            return
        self._waiting[user_id] = None
        while len(self._waiting) > ENRICH_BACKLOG:
            self._waiting.pop(next(iter(self._waiting)))
        # `done()` and not `self._lookups` emptiness: a worker that has
        # just returned is still in the bucket until asyncio runs its
        # done-callback, and a stranger queued in that window would wait
        # for the next one to arrive.
        if all(task.done() for task in self._lookups):
            tasks.spawn(self._lookups, self._drain())

    async def _drain(self) -> None:
        """Resolve every queued identity, one lookup at a time."""
        while self._waiting:
            user_id, _ = self._waiting.popitem()  # most recent first
            await self._enrich(user_id)

    async def _enrich(self, user_id: int) -> None:
        """Resolve a user's username/name (phone is almost always absent)."""
        peer = await self.deps.account.peer(user_id)
        if peer is None:  # unresolvable id: leave the row bare
            return
        self.deps.store.apply_identity(
            users.Identity(
                user_id,
                username=peer.username or None,
                first_name=peer.first_name or None,
                last_name=peer.last_name or None,
                phone=peer.phone or None,
            )
        )

    async def report(self) -> None:
        """Post the users-DB summary to the source chat (/users command)."""
        await self.deps.account.send(
            self.deps.source, userchat.Text(self.text())
        )
        log.info('sent users report to %s', self.deps.source)

    def text(self) -> str:
        """Return the /users message: totals, top commenters, join/leave."""
        if not self.deps.enabled:
            return 'Users DB: disabled (set users.enabled in the JSON).'
        store = self.deps.store
        summary = store.summary()
        lines = [
            'Users DB',
            (
                f'  total={summary["total"]}'
                f' subscribed={summary["subscribed"]}'
                f' left={summary["left"]} messages={summary["messages"]}'
            ),
        ]
        top = store.top_commenters(REPORT_ROWS)
        if top:
            lines.append('  top commenters:')
            lines += [
                f'    - {user_label(r)}: {r["msg_count"]} msg' for r in top
            ]
        recent = store.recent_events(REPORT_ROWS)
        if recent:
            lines.append('  recent join/leave:')
            lines += [
                f'    - {r["event"]}: {user_label(r)}'
                f' {iso(float(str(r["ts"])))}'
                for r in recent
            ]
        return '\n'.join(lines)
