# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The poster's on-disk shapes: a published post, and a group still waiting.

Each is a ROW now, in ``posted`` or ``pending``, and these four functions are
the schema of it. They are here rather than in ``glue/aggregator.py`` because
they are the FORMAT, not the behaviour: a reader of a saved row should be
able to learn what every column means without reading the posting loop.

Two things stay JSON inside a row, and it is a distinction worth keeping
straight. ``links`` is a platform->url map and ``items`` a platform->item
map, both belonging to ONE publication -- composite values of their row, not
collections of entities anything queries across. The rule that emptied the
state blob is about plurals that grow with the world; these grow with the
number of platforms, which is four.

Time is an epoch here, as everywhere else in the file. It used to be an ISO
string in this one place, so the re-post guard parsed a date on every
comparison and a corrupt one had to be given a meaning. ISO is a RENDERING
and lives in ``glue/status.py``, where somebody reads it.

Storage itself is not here. It used to be -- a hand-rolled JSON file per
service, three of them under three naming conventions, each with its own
write-to-temp-and-rename dance. ``core/state.py`` owns it now.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from minions.userbot.core import codec
from minions.userbot.core.models import Group
from minions.userbot.core.models import Item
from minions.userbot.core.models import Posted

if TYPE_CHECKING:
    import sqlite3


def posted_row(post: Posted) -> tuple[object, ...]:
    """Return a Posted record as the columns of its row."""
    return (
        post.title,
        post.at,
        json.dumps(post.links),
        json.dumps(sorted(post.msg_ids)),
    )


def posted_from_row(row: sqlite3.Row) -> Posted:
    """Rebuild a Posted record from its row."""
    return Posted(
        title=str(row['title']),
        at=float(row['at']),
        links={k: str(v) for k, v in _table(row['links']).items()},
        msg_ids=[codec.whole(i) for i in _list(row['msg_ids'])],
    )


def pending_row(group: Group) -> tuple[object, ...]:
    """Return a pending Group as the columns of its row."""
    items = {
        key: {
            'url': item.url,
            'thumbnail': item.thumbnail,
            'duration': item.duration,
            'msg_id': item.msg_id,
        }
        for key, item in group.items.items()
    }
    return (
        group.title,
        group.created_at,
        json.dumps(items),
        json.dumps(sorted(group.msg_ids)),
    )


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


def pending_from_row(row: sqlite3.Row) -> Group:
    """Rebuild a Group from its row in ``pending``."""
    title = str(row['title'])
    return Group(
        title=title,
        items={
            key: _item(key, title, codec.table(value))
            for key, value in _table(row['items']).items()
        },
        msg_ids={codec.whole(i) for i in _list(row['msg_ids'])},
        created_at=float(row['since']),
    )


def _table(raw: object) -> dict[str, object]:
    """Read one JSON column as an object; an empty one when it is not."""
    try:
        return codec.table(json.loads(str(raw)))
    except (TypeError, ValueError):
        return {}


def _list(raw: object) -> list[object]:
    """Read one JSON column as an array; an empty one when it is not."""
    try:
        return codec.rows(json.loads(str(raw)))
    except (TypeError, ValueError):
        return []
