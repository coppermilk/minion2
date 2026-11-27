# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Complete a post's "watch" grid with the networks that came out later.

The aggregator posts a video when every network arrived or when its
timeout runs out; a network published after that never reached the post.
The site's posts feed (``/posts.json``) is the record of which networks a
sketch has -- the site's own clustering decides what one sketch is -- so
this module compares a channel post with it and plans the edit that adds
the missing cells, in the grid's order (``post.rows``), as the aggregator
would have drawn them.

Pure: the post comes in as ``tgedit.Post``, the plan goes out as offsets.
Nothing is guessed:

* a post is recognised by its cells -- a premium emoji, one space, a
  hidden link to a short video address (``https://<network>.<domain>/``);
  the grid is the stretch from the first cell to the last, and anything in
  it but cells, spaces, the separator and line breaks skips the post;
* the post matches the feed's sketch whose links contain one of its cell
  addresses; none or two sketches: skipped;
* existing cells keep their entities -- the same premium emoji ids, the
  same label, the same underline -- and only move; a new cell copies the
  first cell's link formatting with the feed's address, and carries its
  network's premium emoji (a network without one is skipped: the channel
  carries no cell without an emoji);
* a cell is never removed, a complete post plans nothing, so a second pass
  edits nothing.

Offsets are UTF-16 code units, Telegram's entity unit.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from minions.userbot.core import relink
from minions.userbot.core import tgedit

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Mapping

NETWORKS = ('tiktok', 'youtube', 'instagram', 'pinterest')
"""The video networks a grid cell can point at."""
_UTF16 = 'utf-16-le'

COMPLETE = 'complete'
NOT_A_GRID = 'not_a_grid'
NO_MATCH = 'no_match'
AMBIGUOUS = 'ambiguous'
FORMATTING = 'formatting'
NO_EMOJI = 'no_emoji'
"""Why a post plans no edit; ``COMPLETE`` is the normal quiet answer."""


class FeedError(ValueError):
    """The posts feed cannot be read; the message says why."""


@dataclass(frozen=True)
class Layout:
    """How the grid is drawn, from the constants' ``post`` and ``emoji``."""

    rows: tuple[tuple[str, ...], ...]
    separator: str
    domain: str
    emoji: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    """Network -> (premium emoji id, fallback glyph)."""


@dataclass(frozen=True)
class Plan:
    """One post's outcome: the networks added and the edited post.

    ``spans`` is parallel to the post's entities. ``clones`` are new
    entities copied from an existing one: ``(source index, offset, length,
    address)``, the address '' for a copy that keeps the source's (an
    underline has none). ``emoji`` are the premium emoji of the new cells,
    ``(offset, length, id)``.
    """

    post_id: int
    added: tuple[str, ...] = ()
    reason: str = ''
    text: str = ''
    spans: tuple[tuple[int, int], ...] = ()
    clones: tuple[tuple[int, int, int, str], ...] = ()
    emoji: tuple[tuple[int, int, str], ...] = ()


def parse_feed(text: str) -> tuple[dict[str, str], ...]:
    """Return each sketch's links ``{network: address}`` from ``/posts.json``.

    The feed whole or nothing: a body that does not parse or has no
    ``posts`` list raises ``FeedError``; a post without links is left out.
    """
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f'posts feed is not JSON: {exc.msg} at {exc.pos}'
        raise FeedError(msg) from None
    posts = raw.get('posts') if isinstance(raw, dict) else None
    if not isinstance(posts, list):
        msg = 'posts feed has no "posts" list'
        raise FeedError(msg)
    return tuple(links for links in map(_links_of, posts) if links)


def _links_of(post: object) -> dict[str, str]:
    """Return one feed post's video links by network ({} for none)."""
    links = post.get('links') if isinstance(post, dict) else None
    if not isinstance(links, dict):
        return {}
    return {
        str(k): v
        for k, v in links.items()
        if k in NETWORKS and isinstance(v, str) and v
    }


def network_of(url: str, domain: str) -> str:
    """Return the network of a short video address, or '' for any other."""
    if not domain:
        return ''
    pattern = rf'https://({"|".join(NETWORKS)})\.{re.escape(domain)}/\w+/?'
    match = re.fullmatch(pattern, url.strip())
    return match.group(1) if match else ''


def _same(a: str, b: str) -> bool:
    """Whether two addresses are one (case of the host, a final slash)."""
    return a.strip().rstrip('/').lower() == b.strip().rstrip('/').lower()


@dataclass(frozen=True)
class _Cell:
    """One cell of the grid as read: its network and entity indexes."""

    network: str
    url: str
    emoji: int
    link: int
    extras: tuple[int, ...]
    """Entities exactly over the label besides the link (an underline)."""


class _Units:
    """A text addressed in UTF-16 units, as Telegram addresses entities."""

    def __init__(self, text: str) -> None:
        """Keep the text's UTF-16 bytes."""
        self.raw = text.encode(_UTF16)

    def __len__(self) -> int:
        """Length in units."""
        return len(self.raw) // 2

    def cut(self, offset: int, length: int) -> str:
        """Return the text of (offset, length)."""
        return self.raw[offset * 2 : (offset + length) * 2].decode(
            _UTF16, errors='ignore'
        )


def units(text: str) -> int:
    """Length of ``text`` in UTF-16 code units."""
    return len(text.encode(_UTF16)) // 2


def _cells(post: tgedit.Post, domain: str) -> list[_Cell] | None:
    """Read the post's cells in text order; None when one is malformed."""
    text = _Units(post.text)
    emoji_at = {
        offset + length: i
        for i, (offset, length) in enumerate(post.spans)
        if post.kinds[i] == tgedit.EMOJI
    }
    cells: list[_Cell] = []
    for index, url in sorted(post.links, key=lambda one: post.spans[one[0]]):
        network = network_of(url, domain)
        if not network or post.kinds[index] != relink.HIDDEN:
            continue
        offset, _ = post.spans[index]
        emoji = emoji_at.get(offset - 1)
        if emoji is None or text.cut(offset - 1, 1) != ' ':
            return None
        extras = tuple(
            i
            for i, span in enumerate(post.spans)
            if i != index and span == post.spans[index]
        )
        cells.append(_Cell(network, url, emoji, index, extras))
    return cells


def _region(post: tgedit.Post, cells: list[_Cell], sep: str) -> str:
    """Return why the grid's stretch holds more than cells, or ''."""
    start = post.spans[cells[0].emoji][0]
    end = sum(post.spans[cells[-1].link])
    owned = {i for c in cells for i in (c.emoji, c.link, *c.extras)}
    for index, (offset, length) in enumerate(post.spans):
        overlaps = offset < end and start < offset + length
        if overlaps and index not in owned:
            return FORMATTING
    text = _Units(post.text)
    gaps = []
    cursor = start
    for cell in cells:
        gaps.append(text.cut(cursor, post.spans[cell.emoji][0] - cursor))
        cursor = sum(post.spans[cell.link])
    filler = set(sep) | {' ', '\n'}
    if any(set(gap) - filler for gap in gaps):
        return NOT_A_GRID
    return ''


def _sketch(
    cells: list[_Cell], feed: tuple[dict[str, str], ...]
) -> tuple[dict[str, str] | None, str]:
    """Return the one feed sketch the cells belong to, or (None, why)."""
    hits = [
        links
        for links in feed
        if any(_same(c.url, u) for c in cells for u in links.values())
    ]
    if not hits:
        return None, NO_MATCH
    if len(hits) > 1:
        return None, AMBIGUOUS
    return hits[0], ''


@dataclass
class _Writer:
    """The new grid being written, entity by entity, in units."""

    start: int
    parts: list[str] = field(default_factory=list)
    at: int = 0
    moved: dict[int, tuple[int, int]] = field(default_factory=dict)
    clones: list[tuple[int, int, int, str]] = field(default_factory=list)
    emoji: list[tuple[int, int, str]] = field(default_factory=list)

    def put(self, piece: str) -> tuple[int, int]:
        """Append text; return its absolute (offset, length)."""
        where = (self.start + self.at, units(piece))
        self.parts.append(piece)
        self.at += where[1]
        return where


def _grid(post: tgedit.Post, layout: Layout) -> tuple[list[_Cell], str]:
    """Return the post's cells, or why the post has no grid to complete."""
    cells = _cells(post, layout.domain)
    if not cells or len({c.network for c in cells}) != len(cells):
        return [], NOT_A_GRID
    return cells, _region(post, cells, layout.separator)


def _added(
    cells: list[_Cell], sketch: dict[str, str], layout: Layout
) -> tuple[str, ...]:
    """Return the sketch's networks the grid lacks, in the grid's order."""
    have = {c.network for c in cells}
    order = [n for row in layout.rows for n in row]
    return tuple(n for n in order if n in sketch and n not in have)


def _why_not(added: tuple[str, ...], layout: Layout) -> str:
    """Return why nothing is added: complete, or a network without emoji."""
    if not added:
        return COMPLETE
    if any(n not in layout.emoji for n in added):
        return NO_EMOJI
    return ''


def plan(
    post: tgedit.Post, feed: tuple[dict[str, str], ...], layout: Layout
) -> Plan:
    """Plan the edit that adds the feed's missing networks to ``post``."""
    cells, why = _grid(post, layout)
    sketch, why = (None, why) if why else _sketch(cells, feed)
    added = _added(cells, sketch or {}, layout)
    why = why or _why_not(added, layout)
    if why:
        return Plan(post.id, reason=why)
    return _Redraw(post, cells, sketch or {}, layout).plan(added)


@dataclass
class _Redraw:
    """The grid drawn again: old cells moved, new cells copied from one."""

    post: tgedit.Post
    cells: list[_Cell]
    sketch: dict[str, str]
    layout: Layout

    def __post_init__(self) -> None:
        """Fix the grid's stretch and the cell every new one copies."""
        self.text = _Units(self.post.text)
        self.have = {c.network: c for c in self.cells}
        self.template = self.cells[0]
        self.start = self.post.spans[self.cells[0].emoji][0]
        self.end = sum(self.post.spans[self.cells[-1].link])

    def label(self, network: str) -> str:
        """Return a cell's label: its own, or the template's for a new one."""
        cell = self.have.get(network, self.template)
        return self.text.cut(*self.post.spans[cell.link])

    def plan(self, added: tuple[str, ...]) -> Plan:
        """Write the new grid and shift every entity after it."""
        rows = [
            [n for n in row if n in self.have or n in added]
            for row in self.layout.rows
        ]
        rows = [row for row in rows if row]
        out = _Writer(self.start)
        for number, row in enumerate(rows):
            if number:
                out.put('\n')
            self._row(out, row, _widths(rows, self.label))
        delta = out.at - (self.end - self.start)
        spans = [
            out.moved.get(i, (o, n) if o < self.start else (o + delta, n))
            for i, (o, n) in enumerate(self.post.spans)
        ]
        tail = self.text.cut(self.end, len(self.text) - self.end)
        return Plan(
            self.post.id,
            added=added,
            text=self.text.cut(0, self.start) + ''.join(out.parts) + tail,
            spans=tuple(spans),
            clones=tuple(out.clones),
            emoji=tuple(out.emoji),
        )

    def _row(
        self, out: _Writer, row: list[str], widths: dict[int, int]
    ) -> None:
        """Write one row; the separators line up, the last cell is bare."""
        for col, network in enumerate(row):
            if network in self.have:
                self._move(out, self.have[network])
            else:
                self._copy(out, network)
            if col != len(row) - 1:
                pad = ' ' * (widths[col] - len(self.label(network)))
                out.put(pad + self.layout.separator)

    def _move(self, out: _Writer, cell: _Cell) -> None:
        """Write an existing cell: its own glyph, label and entities."""
        glyph = self.text.cut(*self.post.spans[cell.emoji])
        out.moved[cell.emoji] = out.put(glyph)
        out.put(' ')
        where = out.put(self.label(cell.network))
        for index in (cell.link, *cell.extras):
            out.moved[index] = where

    def _copy(self, out: _Writer, network: str) -> None:
        """Write a new cell: its network's emoji, the template's formatting."""
        ident, glyph = self.layout.emoji[network]
        out.emoji.append((*out.put(glyph), ident))
        out.put(' ')
        where = out.put(self.label(network))
        url = self.sketch[network]
        out.clones.append((self.template.link, *where, url))
        out.clones.extend((i, *where, '') for i in self.template.extras)


def _widths(
    rows: list[list[str]], label: Callable[[str], str]
) -> dict[int, int]:
    """Return the longest label of each column (to align the separator)."""
    widths: dict[int, int] = {}
    for row in rows:
        for col, network in enumerate(row):
            widths[col] = max(widths.get(col, 0), len(label(network)))
    return widths
