# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""How state reaches disk: the shared store, plus the poster's shapes.

Every engine whose state is a handful of scalars persists through
``read_state`` / ``write_state``, so the CT-A invariant is proven in one
place: the watchdog turns a hang into a hard ``os._exit(1)``, which makes a
half-written state file a case that happens.

The blob lands in SQLite, one row in one table, rather than in a JSON file.
Not for the query language -- there is nothing to query in a dozen scalars
-- but so the state directory has ONE shape. It used to have three: a
shared peers.db keyed by an engine column, a shared cursors.json, and three
hand-rolled JSON files under three different naming conventions. Nothing in
a listing said which service owned what. Now the file is named for its
service and holds only that service's state.

A write is one transaction, which is atomic by construction -- the ``.tmp``
rename this used to need is what SQLite does for us.

The Posted/Group codecs below are the poster's own on-disk schema.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import TYPE_CHECKING

from minions.userbot.core import codec
from minions.userbot.core import state
from minions.userbot.core.models import Group
from minions.userbot.core.models import Item
from minions.userbot.core.models import Posted
from minions.userbot.core.models import iso
from minions.userbot.core.models import parse_iso

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


_BLOB_SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
    id    INTEGER PRIMARY KEY CHECK (id = 1),
    blob  TEXT    NOT NULL
);
"""
"""One row, holding the whole blob. The CHECK is the schema saying out loud
that there is exactly one of these, so a second row cannot be written."""


def _connect(path: Path) -> sqlite3.Connection:
    """Open a blob store, applying the schema and this repo's journal mode."""
    conn = sqlite3.connect(str(path))
    conn.executescript(_BLOB_SCHEMA)
    conn.execute(f'PRAGMA journal_mode={state.JOURNAL}')
    conn.commit()
    return conn


def adopt(path: Path, legacy: Path) -> None:
    """Import a pre-SQLite JSON state file, once, then set it aside.

    The caller names its own former file because only it knows what that
    was: three services had three naming conventions between them. Does
    nothing when the store already holds a row, so a restart cannot undo
    later state by re-importing a stale file, and nothing when there is no
    legacy file to read.
    """
    if not legacy.exists() or read_state(path):
        return
    try:
        data = json.loads(legacy.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    write_state(path, data)
    legacy.rename(legacy.with_suffix(legacy.suffix + '.bak'))


def read_state(path: Path) -> dict[str, object]:
    """Return a state store's contents, or ``{}`` if there is none to read.

    Missing, unreadable and not-an-object all mean "start from your
    defaults". A caller for whom that would silently discard history reads
    the store itself instead (see the poster's ``restore``).
    """
    try:
        conn = _connect(path)
    except sqlite3.Error:
        return {}
    try:
        got = conn.execute('SELECT blob FROM state WHERE id = 1').fetchone()
    finally:
        conn.close()
    if got is None:
        return {}
    try:
        data = json.loads(got[0])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def read_state_strict(path: Path) -> dict[str, object]:
    """Return a state store's contents, raising rather than degrading.

    For the caller whose empty result would be a LIE: the poster reading
    "nothing was ever posted" disarms its re-post guard and republishes the
    backlog. Better to fail loudly and let the watchdog restart us.
    """
    conn = _connect(path)
    try:
        got = conn.execute('SELECT blob FROM state WHERE id = 1').fetchone()
    finally:
        conn.close()
    if got is None:
        return {}
    data = json.loads(got[0])
    if not isinstance(data, dict):
        msg = f'{path.name}: state is not an object'
        raise TypeError(msg)
    return data


def write_state(path: Path, data: Mapping[str, object]) -> None:
    """Persist a state store's contents; the transaction is the atomicity.

    A kill mid-write leaves the previous row whole, which is what the old
    write-and-rename dance bought by hand.
    """
    conn = _connect(path)
    try:
        conn.execute(
            'INSERT INTO state (id, blob) VALUES (1, ?) '
            'ON CONFLICT (id) DO UPDATE SET blob = excluded.blob',
            (json.dumps(data, ensure_ascii=False),),
        )
        conn.commit()
    finally:
        conn.close()


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
