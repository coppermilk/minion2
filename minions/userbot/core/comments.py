# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""How many comments the channel's recent posts have, as the site reads it.

The site's ``telegram`` tool reads the channel's public web page, and that
page shows reactions and views but no comment count; the team's goal for a
post about a new video is three comments (task 213). The account sees the
count on every post it reads (``Msg.comments``), so the userbot writes it
to ``outbox/tg-comments.json`` and nas-update relays the file to the site's
inbox (kind ``tg-comments``) whenever a count changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from minions.userbot.core.models import iso

if TYPE_CHECKING:
    from collections.abc import Sequence

    from minion_core.adapters.userchat import Msg

OUTBOX_FILE = 'tg-comments.json'
# The site's inbox body format (workers/go/src/inbox.mjs, BODY_VERSION).
BODY_VERSION = 1


def counts(posts: Sequence[Msg]) -> dict[str, int]:
    """Comment count per post id, newest first, for posts that take them."""
    return {
        str(post.id): post.comments
        for post in sorted(posts, key=lambda p: p.id, reverse=True)
        if post.id and post.comments is not None
    }


def body(per_post: dict[str, int], now: float) -> dict[str, object]:
    """Build the inbox body of ``tg-comments``.

    ``discussion`` is False when not one post takes comments -- the channel
    has no linked group, or comments are off -- so the site can say why it
    has no counts instead of showing zeros.
    """
    return {
        'v': BODY_VERSION,
        'generated_at': iso(now),
        'discussion': bool(per_post),
        'posts': per_post,
    }
