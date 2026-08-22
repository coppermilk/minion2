# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Serialize/deserialize the aggregator's posted + pending state.

Extracted from ``main``: the readable JSON shapes for a Posted record and a
pending Group, and their inverses. Pure functions over the models, so the
on-disk schema lives in one place.
"""

from __future__ import annotations

import time

from minions.aggregator.core.models import Group
from minions.aggregator.core.models import Item
from minions.aggregator.core.models import Posted
from minions.aggregator.core.models import _iso
from minions.aggregator.core.models import _parse_iso


def _posted_dict(post: Posted) -> dict[str, object]:
    """Return a Posted record as a readable JSON dict."""
    return {
        'title': post.title,
        'at': post.at,
        'links': post.links,
        'msg_ids': sorted(post.msg_ids),
    }


def _posted_from_dict(raw: dict[str, object]) -> Posted:
    """Rebuild a Posted record from its dict."""
    return Posted(
        title=str(raw.get('title', '')),
        at=str(raw.get('at', '')),
        links=dict(raw.get('links') or {}),
        msg_ids=[int(i) for i in (raw.get('msg_ids') or [])],
    )


def _pending_dict(
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
        'since': _iso(group.created_at),
        'waiting': [p for p in platforms if p not in group.items],
        'items': items,
        'msg_ids': sorted(group.msg_ids),
    }


def _pending_from_dict(raw: dict[str, object]) -> Group:
    """Rebuild a Group from a pending dict (or an old-schema group dict)."""
    title = str(raw.get('title', ''))
    items = {
        key: Item(
            key=key,
            platform=key,
            title=title,
            url=str(value.get('url', '')),
            thumbnail=str(value.get('thumbnail', '')),
            duration=str(value.get('duration', '')),
            msg_id=int(value.get('msg_id', 0)),
        )
        for key, value in (raw.get('items') or {}).items()
    }
    since = raw.get('since')
    created_at = (
        _parse_iso(str(since))
        if since is not None
        else float(raw.get('created_at') or time.time())
    )
    return Group(
        title=title,
        items=items,
        msg_ids=set(raw.get('msg_ids') or []),
        created_at=created_at,
    )
