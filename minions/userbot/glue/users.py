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

from minions.userbot.core import tasks
from minions.userbot.core.matching import thread_top
from minions.userbot.core.models import iso
from minions.userbot.engines import users

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable

    from telethon import TelegramClient
    from telethon import events

log = logging.getLogger('userbot')

# How many rows each /users section lists.
REPORT_ROWS = 5


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

    client: TelegramClient
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
    _lookups: set[asyncio.Task[None]] = field(default_factory=set)

    def record_message(self, event: events.NewMessage.Event) -> None:
        """Log a seen audience message (a source or discussion comment).

        Records non-own messages in the source chat or a watched discussion
        group -- the chats the account actually sees -- bumping the sender's
        count and storing the text unless ``store_text`` is off, then
        enriching the sender's identity lazily. Idempotent per (chat, msg_id).
        """
        message = event.message
        if not self.deps.enabled or getattr(message, 'out', False):
            return
        uid = int(getattr(event, 'sender_id', 0) or 0)
        chat = int(event.chat_id or 0)
        if uid <= 0 or (
            chat != self.deps.source and chat not in self.deps.watched()
        ):
            return
        body = str(getattr(message, 'message', '') or '')
        self.deps.store.record_message(
            users.SeenMessage(
                uid,
                chat,
                int(getattr(message, 'id', 0) or 0),
                root=int(thread_top(getattr(message, 'reply_to', None)) or 0),
                text=body if self.deps.store_text else '',
            )
        )
        self._maybe_enrich(uid)

    def note_membership(self, event: tuple[int, int, bool, bool]) -> None:
        """Greeter sink: persist a join/leave (idempotent on admin_log_id)."""
        admin_log_id, user_id, joined, left = event
        if not self.deps.enabled or user_id <= 0:
            return
        self.deps.store.record_membership(
            users.MembershipEvent(
                user_id, joined=joined, left=left, admin_log_id=admin_log_id
            )
        )
        self._maybe_enrich(user_id)

    def close(self) -> None:
        """Drop in-flight lookups and release the SQLite handle."""
        tasks.cancel_all(self._lookups)
        self.deps.store.close()

    def _maybe_enrich(self, user_id: int) -> None:
        """Schedule a one-off identity lookup for a user we do not know yet."""
        if (
            not self.deps.enrich
            or user_id <= 0
            or self.deps.store.has_identity(user_id)
        ):
            return
        tasks.spawn(self._lookups, self._enrich(user_id))

    async def _enrich(self, user_id: int) -> None:
        """Resolve a user's username/name (phone is almost always absent)."""
        try:
            entity = await self.deps.client.get_entity(user_id)
        except Exception:  # noqa: BLE001 -- unresolvable id: leave it bare
            return
        self.deps.store.apply_identity(
            users.Identity(
                user_id,
                username=getattr(entity, 'username', None),
                first_name=getattr(entity, 'first_name', None),
                last_name=getattr(entity, 'last_name', None),
                phone=getattr(entity, 'phone', None),
            )
        )

    async def report(self) -> None:
        """Post the users-DB summary to the source chat (/users command)."""
        await self.deps.client.send_message(self.deps.source, self.text())
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
