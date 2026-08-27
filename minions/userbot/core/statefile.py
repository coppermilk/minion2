# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""How state reaches disk: the shared atomic IO, plus the poster's shapes.

Every engine persists through ``read_state`` / ``write_state``, so the CT-A
invariant is proven in one place: the watchdog turns a hang into a hard
``os._exit(1)``, which makes a half-written state file a case that happens.
The Posted/Group codecs below are the poster's own on-disk schema.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from minions.userbot.core.models import Group
from minions.userbot.core.models import Item
from minions.userbot.core.models import Posted
from minions.userbot.core.models import iso
from minions.userbot.core.models import parse_iso

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


def read_state(path: Path) -> dict[str, object]:
    """Return a state file's contents, or ``{}`` if there is none to read.

    Missing, unreadable and not-an-object all mean "start from your
    defaults". A caller for whom that would silently discard history reads
    the file itself instead (see the poster's ``restore``).
    """
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):  # JSONDecodeError is a ValueError
        return {}
    return data if isinstance(data, dict) else {}


def write_state(path: Path, data: Mapping[str, object]) -> None:
    """Persist a state file atomically, as readable UTF-8 JSON.

    Via a sibling ``.tmp``, so a kill mid-write leaves the old state whole.
    """
    tmp = path.with_suffix('.tmp')
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    tmp.replace(path)


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
        links=dict(raw.get('links') or {}),
        msg_ids=[int(i) for i in (raw.get('msg_ids') or [])],
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


def pending_from_dict(raw: dict[str, object]) -> Group:
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
        parse_iso(str(since))
        if since is not None
        else float(raw.get('created_at') or time.time())
    )
    return Group(
        title=title,
        items=items,
        msg_ids=set(raw.get('msg_ids') or []),
        created_at=created_at,
    )
