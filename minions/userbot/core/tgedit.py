# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Apply an approved package of channel edits: the pure half.

The team's board carries a package of edits to the channel's posts -- a
note tagged ``tg-edit`` with one JSON block::

    [{"post": 1167, "action": "edit", "old": "exact text", "new": "..."},
     {"post": 1164, "action": "link", "old": "https://a", "new": "https://b"},
     {"post": 1142, "action": "delete"},
     {"post": 0, "action": "desc", "new": "appended to the about"},
     {"post": 0, "action": "pin_new", "new": "a new post, pinned"}]

This module parses that block, decides whether the package is approved,
and dry-runs every item against the post as it is NOW. It never touches
Telegram or the board, so every rule of the protocol is under a test:

* **approval** -- a note ``<word> #<package id>`` by an approver role that
  is not the package's author (nobody accepts their own work);
* **dry run first** -- ``old`` must occur in the post's current text exactly
  once, character for character; a link's ``old`` must be the address of
  at least one of its links. Anything else is skipped, never guessed;
* **limits** -- at most ``MAX_DELETES`` deletions per package (the whole
  package is refused above that) and writes ``MIN_PAUSE_SEC`` apart;
* **never touched** -- a protected address (the chat), the protected link
  rows and every premium emoji (the "watch" buttons), and any post that is
  not in the package. An edit whose fragment cuts into formatting is
  skipped rather than allowed to shift a glyph onto the wrong letter.

Offsets are UTF-16 code units, Telegram's entity unit, as in ``relink``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from dataclasses import field

from minions.userbot.core import relink

EDIT = 'edit'
DELETE = 'delete'
LINK = 'link'
DESC = 'desc'
PIN_NEW = 'pin_new'
ACTIONS = (EDIT, DELETE, LINK, DESC, PIN_NEW)
CHANNEL_ACTIONS = (DESC, PIN_NEW)
"""Actions on the channel itself; they carry ``post`` 0."""

MAX_DELETES = 15
"""Hard ceiling on deletions per package; the config may only lower it."""
MIN_PAUSE_SEC = 10.0
"""Hard floor between two writes; the config may only raise it."""
ABOUT_LIMIT = 255
"""Telegram's limit on a channel description, in characters."""

APPLIED = 'applied'
SKIPPED = 'skipped'
ERROR = 'error'
DRY_OK = 'dry_ok'
"""Item outcomes: done, refused by a rule, failed in Telegram, would apply."""

NOT_FOUND = 'not_found'
AMBIGUOUS = 'ambiguous'
NO_POST = 'no_post'
PROTECTED = 'protected'
FORMATTING = 'formatting'
NOT_EMPTY = 'not_empty'
ALREADY = 'already'
TOO_LONG = 'too_long'
NO_RIGHTS = 'no_rights'
CONFLICT = 'conflict'
FAILED = 'failed'
"""Why an item was skipped or failed; the config maps each to words."""

EMOJI = 'emoji'
"""The entity kind of a premium (custom) emoji."""

_KEYS = frozenset({'post', 'action', 'old', 'new', 'only_if_empty'})
_BLOCK = re.compile(r'```(?:json)?\s*\n(.*?)\n?```', re.DOTALL)
_UTF16 = 'utf-16-le'


class PackageError(ValueError):
    """The package cannot be applied at all; the message says why."""


@dataclass(frozen=True)
class Item:
    """One edit of the package, as written on the board."""

    post: int
    action: str
    old: str = ''
    new: str = ''
    only_if_empty: bool = False


@dataclass(frozen=True)
class Outcome:
    """What happened (or would happen) to one item, and why.

    ``count`` is how many places the item changes: a ``link`` moves every
    link with its address (post 1045 has two), anything else one.
    """

    item: Item
    status: str
    reason: str = ''
    count: int = 1


@dataclass(frozen=True)
class Guard:
    """What the editor never touches, from ``engines.tgedit``."""

    urls: tuple[str, ...] = ()
    link_texts: tuple[str, ...] = ()

    def names(self, text: str) -> bool:
        """Whether ``text`` names a protected address."""
        return any(url and url in text for url in self.urls)


@dataclass(frozen=True)
class Post:
    """One post as read: text, every entity's span and kind, its links.

    ``kinds`` is parallel to ``spans`` (``EMOJI``, ``relink.HIDDEN``,
    ``relink.VISIBLE`` or anything else); ``links`` index into ``spans``.
    """

    id: int
    text: str
    spans: tuple[tuple[int, int], ...]
    kinds: tuple[str, ...]
    links: tuple[tuple[int, str], ...]
    media: bool = False


@dataclass(frozen=True)
class PostPlan:
    """One post after the dry run: each item's outcome and the new post.

    ``text``/``spans`` are the post once every passing item is applied;
    ``urls`` maps an entity index to its new address. ``delete`` is set
    when the one item is a passing deletion.
    """

    post_id: int
    outcomes: tuple[Outcome, ...]
    text: str = ''
    spans: tuple[tuple[int, int], ...] = ()
    urls: dict[int, str] = field(default_factory=dict)
    delete: bool = False

    @property
    def changes(self) -> bool:
        """Whether anything passed the dry run (a write is due)."""
        return any(one.status == DRY_OK for one in self.outcomes)


# ------------------------------------------------------------- parsing


def parse(text: str) -> tuple[Item, ...]:
    """Read the JSON block of a package note into items.

    Raises ``PackageError`` naming the first problem: no block, broken
    JSON, an unknown key or action, a missing field. A package is applied
    whole or not at all, so a typo stops it before anything is touched.
    """
    found = _BLOCK.search(text)
    if found is None:
        msg = 'no ```json block in the note'
        raise PackageError(msg)
    try:
        raw = json.loads(found.group(1))
    except json.JSONDecodeError as exc:
        msg = f'the JSON block does not parse: {exc.msg} at {exc.pos}'
        raise PackageError(msg) from exc
    if not isinstance(raw, list) or not raw:
        msg = 'the JSON block must be a non-empty list of edits'
        raise PackageError(msg)
    return tuple(_item(n, one) for n, one in enumerate(raw, start=1))


def _fail(n: int, why: str) -> PackageError:
    """Build the error for edit ``n``."""
    return PackageError(f'edit {n}: {why}')


def _item(n: int, raw: object) -> Item:
    """Validate one edit; ``n`` numbers it in the error."""
    if not isinstance(raw, dict):
        raise _fail(n, 'not an object')
    unknown = set(raw) - _KEYS
    if unknown:
        raise _fail(n, f'unknown key(s) {sorted(unknown)}')
    item = Item(
        post=_whole(n, raw.get('post')),
        action=_action(n, raw.get('action')),
        old=_text(n, raw.get('old', '')),
        new=_text(n, raw.get('new', '')),
        only_if_empty=raw.get('only_if_empty', False) is True,
    )
    _require(n, item)
    return item


def _whole(n: int, value: object) -> int:
    """Return a post number (0 = the channel itself)."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _fail(n, 'post must be a whole number >= 0')
    return value


def _action(n: int, value: object) -> str:
    """Return a known action."""
    if not isinstance(value, str) or value not in ACTIONS:
        raise _fail(n, f'action {value!r} is not one of {ACTIONS}')
    return value


def _text(n: int, value: object) -> str:
    """Return an old/new field."""
    if not isinstance(value, str):
        raise _fail(n, 'old and new must be text')
    return value


def _require(n: int, item: Item) -> None:
    """Check the fields each action needs."""
    on_channel = item.action in CHANNEL_ACTIONS
    if on_channel != (item.post == 0):
        raise _fail(
            n, f'{item.action} needs post {"0" if on_channel else ">0"}'
        )
    needs_old = item.action in {EDIT, LINK}
    needs_new = item.action in {LINK, DESC, PIN_NEW}
    if (needs_old and not item.old) or (needs_new and not item.new):
        raise _fail(n, f'{item.action} needs a non-empty old/new')


def check_limits(items: tuple[Item, ...], max_deletes: int) -> None:
    """Refuse a package with more deletions than allowed (whole package)."""
    cap = min(max(0, max_deletes), MAX_DELETES)
    deletes = sum(1 for one in items if one.action == DELETE)
    if deletes > cap:
        msg = f'{deletes} deletions in one package, at most {cap}'
        raise PackageError(msg)


# ------------------------------------------------------------ approval


@dataclass(frozen=True)
class Note:
    """A board note, as far as approval cares: who wrote what, and when."""

    id: int
    role: str
    text: str
    created_at: str = ''


@dataclass(frozen=True)
class Approval:
    """Who may approve a package, and the word that does it."""

    word: str
    approvers: tuple[str, ...]

    def by(self, package: Note, notes: list[Note]) -> str:
        """Return the role that approved ``package``; '' when none did.

        An approval is a line opening with ``<word> #<id>``
        (case-insensitive) by one of ``approvers``, written after the
        package, by a role other than the package's author. Only the start
        of a line counts, so "not approved #475" approves nothing.
        """
        if not self.word:
            return ''
        mark = re.compile(
            rf'^\s*{re.escape(self.word)}\s*#{package.id}(?!\d)',
            re.IGNORECASE | re.MULTILINE,
        )
        for note in notes:
            if self._counts(package, note) and mark.search(note.text):
                return note.role
        return ''

    def _counts(self, package: Note, note: Note) -> bool:
        """Whether ``note`` is by someone allowed to approve ``package``."""
        if note.id == package.id or note.role == package.role:
            return False
        later = not package.created_at or note.created_at >= package.created_at
        return later and note.role in self.approvers


# -------------------------------------------------------------- dry run


def _units(text: str) -> int:
    """Length of ``text`` in UTF-16 code units."""
    return len(text.encode(_UTF16)) // 2


def _overlaps(span: tuple[int, int], where: tuple[int, int]) -> bool:
    """Whether a span shares a unit with the (offset, length) ``where``."""
    (offset, length), (at, size) = span, where
    return offset < at + size and at < offset + length


def _inside(span: tuple[int, int], where: tuple[int, int]) -> bool:
    """Whether the (offset, length) ``where`` lies wholly inside ``span``."""
    (offset, length), (at, size) = span, where
    return offset <= at and at + size <= offset + length


@dataclass
class _Draft:
    """A post being edited in memory, item by item."""

    post: Post
    guard: Guard
    text: str = ''
    spans: list[tuple[int, int]] = field(default_factory=list)
    urls: dict[int, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Start from the post as read."""
        self.text = self.post.text
        self.spans = list(self.post.spans)

    def shown(self, index: int) -> str:
        """Return the text an entity currently covers."""
        offset, length = self.spans[index]
        units = self.text.encode(_UTF16)
        return units[offset * 2 : (offset + length) * 2].decode(
            _UTF16, errors='ignore'
        )

    def url(self, index: int, read: str) -> str:
        """Return a link entity's current address."""
        if self.post.kinds[index] == relink.VISIBLE:
            return self.shown(index)
        return self.urls.get(index, read)

    def locked(self, index: int) -> bool:
        """Whether an entity is never touched: emoji or a protected link."""
        kind = self.post.kinds[index]
        if kind == EMOJI:
            return True
        links = dict(self.post.links)
        if index not in links:
            return False
        shown = self.shown(index)
        return self.guard.names(self.url(index, links[index])) or any(
            word and word in shown for word in self.guard.link_texts
        )

    def splice(self, where: tuple[int, int], new: str) -> None:
        """Replace the (offset, length) ``where``, shifting every span."""
        got = relink.splice(
            relink.Edit(self.text, tuple(self.spans)), where, new
        )
        self.text, self.spans = got.text, list(got.spans)

    def edit(self, item: Item) -> str:
        """Dry-run and apply one text edit; return '' or why it is skipped."""
        if self.guard.names(item.old) or self.guard.names(item.new):
            return PROTECTED
        count = self.text.count(item.old)
        if count != 1:
            return NOT_FOUND if count == 0 else AMBIGUOUS
        where = (
            _units(self.text[: self.text.index(item.old)]),
            _units(item.old),
        )
        why = self._cuts(where, removes=not item.new)
        if not why:
            self.splice(where, item.new)
        return why

    def _cuts(self, where: tuple[int, int], *, removes: bool) -> str:
        """Return why replacing ``where`` would break an entity, or ''.

        A fragment may sit inside an entity (a word inside bold) or clear
        of it; one that cuts across an entity's edge, swallows a whole
        entity, or touches a locked one is refused.
        """
        for index, span in enumerate(self.spans):
            if not _overlaps(span, where):
                continue
            if self.locked(index):
                return PROTECTED
            if not _inside(span, where) or (span == where and removes):
                return FORMATTING
        return ''

    def link(self, item: Item) -> tuple[str, int]:
        """Dry-run and apply one address change: ('', links moved) or why.

        Every link whose address is exactly ``old`` moves; unlike a text
        fragment, two equal addresses are one target, not an ambiguity.
        """
        if self.guard.names(item.old) or self.guard.names(item.new):
            return PROTECTED, 0
        hits = [
            i for i, read in self.post.links if self.url(i, read) == item.old
        ]
        if not hits:
            return NOT_FOUND, 0
        if any(self.locked(i) for i in hits):
            return PROTECTED, 0
        for index in sorted(hits, key=lambda i: self.spans[i], reverse=True):
            self._readdress(index, item.new)
        return '', len(hits)

    def _readdress(self, index: int, new: str) -> None:
        """Point one link at ``new``: a visible one changes its text too."""
        if self.post.kinds[index] == relink.VISIBLE:
            self.splice(self.spans[index], new)
        else:
            self.urls[index] = new


def plan_post(
    post: Post | None, items: tuple[Item, ...], guard: Guard
) -> PostPlan:
    """Dry-run every item of ONE post against its current state.

    Items apply in package order to the post as the earlier ones left it,
    so two fragments of the same post both land. A deletion must be the
    post's only item: deleting and editing one post is a contradiction.
    """
    if post is None:
        return _skip(items, NO_POST)
    if any(one.action == DELETE for one in items):
        if len(items) > 1:
            return _skip(items, CONFLICT)
        return _plan_delete(post, items[0], guard)
    draft = _Draft(post, guard)
    outcomes: list[Outcome] = []
    for item in items:
        why, count = (
            (draft.edit(item), 1) if item.action == EDIT else draft.link(item)
        )
        outcomes.append(
            Outcome(item, SKIPPED, why)
            if why
            else Outcome(item, DRY_OK, count=count)
        )
    return PostPlan(
        post.id, tuple(outcomes), draft.text, tuple(draft.spans), draft.urls
    )


def _plan_delete(post: Post, item: Item, guard: Guard) -> PostPlan:
    """Dry-run one deletion."""
    names = guard.names(post.text) or any(
        guard.names(url) for _, url in post.links
    )
    if names:
        return _skip((item,), PROTECTED)
    if item.only_if_empty and (post.text.strip() or post.media):
        return _skip((item,), NOT_EMPTY)
    return PostPlan(post.id, (Outcome(item, DRY_OK),), delete=True)


def _skip(items: tuple[Item, ...], reason: str) -> PostPlan:
    """Return a plan that skips every item of one post for one reason."""
    return PostPlan(
        items[0].post, tuple(Outcome(one, SKIPPED, reason) for one in items)
    )


def plan_about(about: str, item: Item, guard: Guard) -> tuple[str, str]:
    """Dry-run a description append: (new about, '') or ('', why)."""
    if guard.names(item.new):
        return '', PROTECTED
    if about.endswith(item.new):
        return '', ALREADY
    after = about + item.new
    if len(after) > ABOUT_LIMIT:
        return '', TOO_LONG
    return after, ''


def plan_pin(recent: list[str], item: Item, guard: Guard) -> str:
    """Dry-run a new pinned post: '' when it may go out, else why not."""
    if guard.names(item.new):
        return PROTECTED
    if item.new.strip() in {one.strip() for one in recent}:
        return ALREADY
    return ''


def by_post(items: tuple[Item, ...]) -> dict[int, tuple[Item, ...]]:
    """Group the post items (not the channel ones) by post, in order."""
    out: dict[int, list[Item]] = {}
    for item in items:
        if item.action not in CHANNEL_ACTIONS:
            out.setdefault(item.post, []).append(item)
    return {post: tuple(group) for post, group in out.items()}


# --------------------------------------------------------------- report


def report(outcomes: list[Outcome], labels: dict[str, str]) -> list[str]:
    """Render ``post N . action -> outcome (reason)``, one line per item.

    An item that changed more than one place adds ``, replaced N``.
    ``labels`` maps the status and reason codes to the words the board
    reads; a code without a label is shown as is.
    """
    lines: list[str] = []
    for one in outcomes:
        where = (
            labels.get('channel', 'channel')
            if one.item.post == 0
            else (f'{labels.get("post", "post")} {one.item.post}')
        )
        said = labels.get(one.status, one.status)
        why = f' ({labels.get(one.reason, one.reason)})' if one.reason else ''
        times = (
            f', {labels.get("replaced", "replaced")} {one.count}'
            if one.count > 1
            else ''
        )
        lines.append(f'{where} . {one.item.action} -> {said}{why}{times}')
    return lines


def tally(outcomes: list[Outcome]) -> dict[str, int]:
    """Count the outcomes by status, for the report's first line."""
    out: dict[str, int] = {}
    for one in outcomes:
        out[one.status] = out.get(one.status, 0) + 1
    return out
