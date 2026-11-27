# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Write the channel's comment counts to the outbox, on a timer.

The Telethon side of ``core/comments``: every ``COUNT_SEC`` it reads the
channel's last ``POSTS`` posts -- one history read, no thread is opened --
and rewrites ``outbox/tg-comments.json``. nas-update relays the file only
when its meaning changes (``generated_at`` is not part of it), so a quiet
channel sends nothing. A failed read is logged and leaves the last file in
place: the site keeps the last known counts with their time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from minion_core.adapters import userchat
from minion_core.kernel import atomic_write
from minions.userbot.core import comments

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

log = logging.getLogger('userbot')

# A new video comes every few days and the goal is judged 24 h after the
# post, so half-hour counts are fresh enough at one read per tick.
COUNT_SEC = 30 * 60
# The site's telegram tool shows the last 50 posts by default; 30 covers
# every post about a video of the last two months.
POSTS = 30


@dataclass(frozen=True)
class CounterDeps:
    """Everything the counter may reach; nothing else is in scope."""

    account: userchat.Account
    channel: Callable[[], int]
    outbox: Path  # outbox/ of the profile's state dir


@dataclass
class CommentCounter:
    """Keep ``outbox/tg-comments.json`` in step with the channel."""

    deps: CounterDeps

    async def loop(self) -> None:
        """Count forever; one failed pass is logged and retried later."""
        while True:
            try:
                await self.count(time.time())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('comments: count failed; will retry')
            await asyncio.sleep(COUNT_SEC)

    async def count(self, now: float) -> bool:
        """Read the channel's last posts and rewrite the file.

        False (and the old file kept) when the read returned nothing: an
        empty read is a failure here, not a channel without posts.
        """
        posts = await self.deps.account.history(
            self.deps.channel(), userchat.Slice(limit=POSTS)
        )
        if not posts:
            log.warning('comments: channel read returned no posts; file kept')
            return False
        per_post = comments.counts(posts)
        data = json.dumps(comments.body(per_post, now), indent=1)
        atomic_write(self.deps.outbox / comments.OUTBOX_FILE, data.encode())
        log.info(
            'comments: %d post(s) take comments, %d comment(s) in all',
            len(per_post),
            sum(per_post.values()),
        )
        return True
