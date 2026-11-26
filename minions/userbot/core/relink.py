# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Keep the links in the channel's posts short: the pure half.

The ``links`` engine walks the channel and rewrites every raw video address
in a post (``tiktok.com/.../video/<id>``, ``youtube.com/watch?v=...``,
``instagram.com/reel/...``, ``pinterest.com/pin/...``) to the site
shortener's trackable address, computed offline by ``core/shortlinks``.
This module decides WHAT to change and computes the new text; it never
touches Telegram, so every rule here is under a unit test.

Offsets and lengths are in UTF-16 code units, the unit Telegram measures
entities in, so the adapter can hand its entities over and take them back
without converting.

The rules, each a line in the task:

* a hidden link (text_url) changes only its address -- text and every
  other entity stay byte-for-byte the same;
* a visible address (url) is replaced in the text, and every entity that
  sits after it or wraps it is shifted by the change in length;
* the site's own addresses, ``t.me`` and anything that is not a video of
  the four networks are never changed; the latter are listed back as
  ``others`` for the operator;
* a post with nothing to change yields an empty plan, so a second pass
  over the same post is a no-op (idempotence without remembering posts).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from urllib.parse import urlsplit

from minions.userbot.core.shortlinks import short_url

HIDDEN = 'text'
"""A text_url entity: the address is hidden behind words."""
VISIBLE = 'url'
"""A url entity: the address IS the text."""

MODES = ('off', 'dry', 'on')
"""off: idle; dry: report only (the default); on: edit the posts."""
DEFAULT_MODE = 'dry'

_KEEP_HOSTS = ('t.me', 'telegram.me', 'telegram.org')
_UTF16 = 'utf-16-le'


@dataclass(frozen=True)
class Link:
    """One link entity of a post, as the adapter read it."""

    kind: str
    offset: int
    length: int
    url: str


@dataclass(frozen=True)
class Change:
    """One link to rewrite: its index in the post's links, old and new."""

    index: int
    old: str
    new: str


@dataclass(frozen=True)
class Plan:
    """What one post needs: the rewrites, and the links left alone."""

    post_id: int
    changes: tuple[Change, ...] = ()
    others: tuple[str, ...] = ()


@dataclass(frozen=True)
class Post:
    """One post as read: its text, every entity's span, its link entities.

    ``spans`` is the (offset, length) of EVERY entity, links included, in
    the adapter's order; ``links`` are the link entities among them.
    """

    text: str
    spans: tuple[tuple[int, int], ...]
    links: tuple[Link, ...]


@dataclass(frozen=True)
class _Cut:
    """One replacement in the text: where, how long it was, the growth."""

    at: int
    old: int
    delta: int


@dataclass(frozen=True)
class Edit:
    """One post's new text and the shifted (offset, length) of each entity."""

    text: str
    spans: tuple[tuple[int, int], ...]


def _absolute(url: str) -> str:
    """Return ``url`` with a scheme, as a visible link may omit it."""
    return url if '://' in url else f'https://{url}'


def _host(url: str) -> str:
    """Return the lower-case host of a URL without ``www.``, or ''."""
    try:
        host = urlsplit(_absolute(url.strip())).hostname or ''
    except ValueError:
        return ''
    host = host.lower()
    return host.removeprefix('www.')


def _ours(host: str, domain: str) -> bool:
    """Whether a host is the site itself or Telegram (never rewritten)."""
    if host in _KEEP_HOSTS:
        return True
    return bool(domain) and (host == domain or host.endswith('.' + domain))


def rewrite(url: str, domain: str) -> str:
    """Return the short address for a raw video URL, '' when none applies.

    '' means "leave it": the site's own links, Telegram, a mention-like
    address, or anything ``shortlinks`` does not recognise as a video.
    """
    if not domain or not url or url.startswith(('tg:', 'mailto:')):
        return ''
    if _ours(_host(url), domain):
        return ''
    new = short_url(_absolute(url), domain)
    return new if new != _absolute(url) else ''


def plan(post_id: int, links: tuple[Link, ...], domain: str) -> Plan:
    """Decide which links of one post to rewrite and which to report."""
    changes: list[Change] = []
    others: list[str] = []
    for index, link in enumerate(links):
        new = rewrite(link.url, domain)
        if new:
            changes.append(Change(index, link.url, new))
        elif link.url and not _ours(_host(link.url), domain):
            others.append(link.url)
    return Plan(post_id, tuple(changes), tuple(others))


def _units(text: str) -> int:
    """Length of ``text`` in UTF-16 code units."""
    return len(text.encode(_UTF16)) // 2


def _shift(span: tuple[int, int], cut: _Cut) -> tuple[int, int]:
    """Move one (offset, length) span for a replacement at ``at``.

    A span wholly after the replaced range moves by ``delta``; a span that
    contains it (the url entity itself, or bold around it) grows by
    ``delta``; any other span is untouched.
    """
    offset, length = span
    at, old, delta = cut.at, cut.old, cut.delta
    if offset + length <= at:
        return span
    if offset >= at + old:
        return offset + delta, length
    if offset <= at and at + old <= offset + length:
        return offset, length + delta
    return span


def apply(post: Post, changes: tuple[Change, ...]) -> Edit:
    """Return the post's new text and spans once ``changes`` are applied.

    Only VISIBLE links touch the text; they are applied right to left so an
    earlier offset is still valid when its turn comes. The result keeps the
    order of ``post.spans``.
    """
    links = post.links
    edit = Edit(post.text, post.spans)
    visible = [c for c in changes if links[c.index].kind == VISIBLE]
    for change in sorted(
        visible, key=lambda c: links[c.index].offset, reverse=True
    ):
        link = links[change.index]
        edit = splice(edit, (link.offset, link.length), change.new)
    return edit


def splice(edit: Edit, where: tuple[int, int], new: str) -> Edit:
    """Replace the (offset, length) ``where`` with ``new``; shift the spans.

    A span after the cut moves, a span around it grows or shrinks, any
    other span stays -- the one rule both the link keeper and the channel
    editor (``core/tgedit``) rely on to keep formatting in place.
    """
    at, old = where
    units = edit.text.encode(_UTF16)
    units = units[: at * 2] + new.encode(_UTF16) + units[(at + old) * 2 :]
    cut = _Cut(at, old, _units(new) - old)
    spans = tuple(_shift(span, cut) for span in edit.spans)
    return Edit(units.decode(_UTF16), spans)


def report(plans: list[Plan]) -> list[str]:
    """Render the dry-run lines: ``post N: old -> new`` and the others."""
    lines: list[str] = []
    for one in plans:
        lines += [
            f'post {one.post_id}: {c.old} -> {c.new}' for c in one.changes
        ]
        lines += [
            f'post {one.post_id}: left as is {url}' for url in one.others
        ]
    return lines


@dataclass(frozen=True)
class Clock:
    """The persona's day: timezone and the waking window [start, end)."""

    tz_offset_hours: float = 0.0
    wake_start_hour: int = 0
    wake_end_hour: int = 24

    def day(self, ts: float) -> str:
        """Return the persona's local date of ``ts`` (the budget's key)."""
        return self._local(ts).strftime('%Y-%m-%d')

    def awake(self, ts: float) -> bool:
        """Whether ``ts`` falls inside the persona's waking window."""
        hour = self._local(ts).hour
        start, end = self.wake_start_hour, self.wake_end_hour
        if start == end:
            return True
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def _local(self, ts: float) -> datetime:
        """Return ``ts`` in the persona's timezone."""
        tz = timezone(timedelta(hours=self.tz_offset_hours))
        return datetime.fromtimestamp(ts, tz=tz)


@dataclass(frozen=True)
class Budget:
    """Edits spent on one local day; a new day starts from zero."""

    day: str = ''
    used: int = 0

    def left(self, today: str, cap: int) -> int:
        """Return how many edits today still allows (cap <= 0: none)."""
        spent = self.used if self.day == today else 0
        return max(0, cap - spent)

    def spend(self, today: str) -> Budget:
        """Return the budget after one more edit today."""
        spent = self.used if self.day == today else 0
        return Budget(today, spent + 1)


def due(posted: float, now: float, delay_sec: float) -> bool:
    """Whether a post is old enough to touch (fresh posts wait a while)."""
    return now - posted >= delay_sec
