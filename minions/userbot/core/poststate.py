# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The poster's on-disk shapes: a published post, and a group still waiting.

The poster's state block (``core/state.StateStore.read``/``write``) is a JSON
object, and these four functions are the schema of what goes in it. They are
here rather than in ``glue/aggregator.py`` because they are the FORMAT, not
the behaviour: a reader of a saved block should be able to learn what every
key means without reading the posting loop.

Storage itself is not here any more. It used to be -- a hand-rolled JSON file
per service, three of them under three naming conventions, each with its own
write-to-temp-and-rename dance. All of that is one row in one table now, in
one database, and ``core/state.py`` owns it.
"""

from __future__ import annotations

import time

from minions.userbot.core import codec
from minions.userbot.core.models import Group
from minions.userbot.core.models import Item
from minions.userbot.core.models import Posted
from minions.userbot.core.models import iso
from minions.userbot.core.models import parse_iso


def posted_dict(post: Posted) -> dict[str, object]:
    """Return a Posted record as a readable JSON dict."""
    return {
        'title': post.title,
        'at': post.at,
        'links': post.links,
        'msg_ids': sorted(post.msg_ids),
    }


def posted_from_dict(raw: dict[str, object]) -> Posted:
    """Rebuild a Posted record from its dict."""
    return Posted(
        title=str(raw.get('title', '')),
        at=str(raw.get('at', '')),
        links={k: str(v) for k, v in codec.table(raw.get('links')).items()},
        msg_ids=[codec.whole(i) for i in codec.rows(raw.get('msg_ids'))],
    )


def pending_dict(
    group: Group, platforms: tuple[str, ...]
) -> dict[str, object]:
    """Return a pending Group as a readable, resumable JSON dict."""
    items = {
        key: {
            'url': item.url,
            'thumbnail': item.thumbnail,
            'duration': item.duration,
            'msg_id': item.msg_id,
        }
        for key, item in group.items.items()
    }
    return {
        'title': group.title,
        'since': iso(group.created_at),
        'waiting': [p for p in platforms if p not in group.items],
        'items': items,
        'msg_ids': sorted(group.msg_ids),
    }


def _item(key: str, title: str, value: dict[str, object]) -> Item:
    """Rebuild one platform's item from its pending block."""
    return Item(
        key=key,
        platform=key,
        title=title,
        url=codec.text(value.get('url')),
        thumbnail=codec.text(value.get('thumbnail')),
        duration=codec.text(value.get('duration')),
        msg_id=codec.whole(value.get('msg_id')),
    )


def pending_from_dict(raw: dict[str, object]) -> Group:
    """Rebuild a Group from a pending dict (or an old-schema group dict)."""
    title = str(raw.get('title', ''))
    items = {
        key: _item(key, title, codec.table(value))
        for key, value in codec.table(raw.get('items')).items()
    }
    since = raw.get('since')
    created_at = (
        parse_iso(str(since))
        if since is not None
        else codec.num(raw.get('created_at')) or time.time()
    )
    return Group(
        title=title,
        items=items,
        msg_ids={codec.whole(i) for i in codec.rows(raw.get('msg_ids'))},
        created_at=created_at,
    )
