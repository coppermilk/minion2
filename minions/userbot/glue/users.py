# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The opt-in users database glue, mixed into Userbot.

Extracted from ``main``: recording seen commenters and join/leave events to
the users DB, lazy identity enrichment, and the /users report. ``_UsersMixin``
is mixed into ``Userbot`` with method bodies unchanged; it inherits
``UserbotProtocol`` (base.py) so the type checker knows the shared state.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from minions.userbot.core.base import UserbotProtocol
from minions.userbot.core.matching import _thread_top
from minions.userbot.core.models import _iso
from minions.userbot.engines import users
from minions.userbot.glue.status import _user_label

if TYPE_CHECKING:
    from telethon import events

log = logging.getLogger('userbot')


class _UsersMixin(UserbotProtocol):
    """The users-DB recording + /users report, mixed into Userbot."""

    def _record_user_message(self, event: events.NewMessage.Event) -> None:
        """Log a seen audience message to the users DB (a discussion comment).

        Records non-own messages in the source chat or a watched discussion
        group (the chats the account actually sees), bumping the sender's count
        and storing the text (unless store_message_text is off), then enriches
        the sender's identity lazily. Idempotent per (chat, msg_id).
        """
        message = event.message
        if not self._users_enabled or getattr(message, 'out', False):
            return
        uid = int(getattr(event, 'sender_id', 0) or 0)
        chat = int(event.chat_id or 0)
        disc_chats = {c for c, _ in self.cats.posts}
        if uid <= 0 or (chat != self.config.source and chat not in disc_chats):
            return
        root = _thread_top(getattr(message, 'reply_to', None)) or 0
        body = str(getattr(message, 'message', '') or '')
        self.users.record_message(
            users.SeenMessage(
                uid,
                chat,
                int(getattr(message, 'id', 0) or 0),
                root=int(root),
                text=body if self._users_store_text else '',
            )
        )
        self._maybe_enrich(uid)

    def _on_membership_event(self, event: tuple[int, int, bool, bool]) -> None:
        """Greeter sink: persist a join/leave to the users DB (idempotent)."""
        admin_log_id, user_id, joined, left = event
        if not self._users_enabled or user_id <= 0:
            return
        self.users.record_membership(
            users.MembershipEvent(
                user_id, joined=joined, left=left, admin_log_id=admin_log_id
            )
        )
        self._maybe_enrich(user_id)

    def _maybe_enrich(self, user_id: int) -> None:
        """Schedule a one-off identity lookup for a user we do not know yet."""
        if (
            not self._users_enrich
            or user_id <= 0
            or self.users.has_identity(user_id)
        ):
            return
        task = asyncio.create_task(self._enrich_user(user_id))
        self._enrich_tasks.add(task)
        task.add_done_callback(self._enrich_tasks.discard)

    async def _enrich_user(self, user_id: int) -> None:
        """Resolve a user's username/name (phone is almost always absent)."""
        try:
            entity = await self.client.get_entity(user_id)
        except Exception:  # noqa: BLE001 -- unresolvable id: leave it bare
            return
        self.users.apply_identity(
            users.Identity(
                user_id,
                username=getattr(entity, 'username', None),
                first_name=getattr(entity, 'first_name', None),
                last_name=getattr(entity, 'last_name', None),
                phone=getattr(entity, 'phone', None),
            )
        )

    async def users_report(self) -> None:
        """Post the users-DB summary to the source chat (/users command)."""
        await self.client.send_message(self.config.source, self._users_text())
        log.info('sent users report to %s', self.config.source)

    def _users_text(self) -> str:
        """Return the /users message: totals, top commenters, join/leave."""
        if not self._users_enabled:
            return 'Users DB: disabled (set users.enabled in the JSON).'
        s = self.users.summary()
        lines = [
            'Users DB',
            (
                f'  total={s["total"]} subscribed={s["subscribed"]}'
                f' left={s["left"]} messages={s["messages"]}'
            ),
        ]
        top = self.users.top_commenters(5)
        if top:
            lines.append('  top commenters:')
            lines += [
                f'    - {_user_label(r)}: {r["msg_count"]} msg' for r in top
            ]
        recent = self.users.recent_events(5)
        if recent:
            lines.append('  recent join/leave:')
            lines += [
                f'    - {r["event"]}: {_user_label(r)}'
                f' {_iso(float(str(r["ts"])))}'
                for r in recent
            ]
        return '\n'.join(lines)
