# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Apply an approved package of channel edits: the pure half.

Packages come from the site's feed (``/tgedit.json``, built from the
site repository's ``data/tgedit/``)::

    {"version": 1,
     "rules": {"max_deletes": 15, "min_pause_sec": 10, ...},
     "packages": [
      {"id": "475-cleanup", "author": "audience-head", "why": "one line",
       "edits": [
        {"post": 1167, "action": "edit", "old": "exact text", "new": "..."},
        {"post": 1164, "action": "link", "old": "https://a", "new": "https://b"},
        {"post": 1142, "action": "delete"},
        {"post": 0, "action": "desc", "new": "appended to the about"},
        {"post": 0, "action": "pin_new", "new": "a new post, pinned"}]}]}

Approval is the merge into the site's ``main``: the site build refuses a
package that breaks this schema, so this module only parses the feed and
dry-runs every item against the post as it is NOW. It never touches the
network or Telegram, so every rule is under a test:

* **dry run first** -- ``old`` must occur in the post's current text exactly
  once, character for character; a link's ``old`` must be the address of
  at least one of its links. Anything else is skipped, never guessed;
* **limits** -- at most ``MAX_DELETES`` deletions per package (the whole
  package is refused above that) and writes ``MIN_PAUSE_SEC`` apart; the
  feed's ``rules`` may only tighten them, never loosen;
* **the feed whole or nothing** -- a feed that does not parse, has another
  ``version``, a broken package or one id twice is refused whole;
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
_PACKAGE_KEYS = frozenset({'id', 'author', 'why', 'edits'})
_ID = re.compile(r'[0-9]+-[a-z0-9]+(?:-[a-z0-9]+)*')
"""A package id: the number of the note it came from, a dash, a word."""
_UTF16 = 'utf-16-le'


class PackageError(ValueError):
    """The package cannot be applied at all; the message says why."""


@dataclass(frozen=True)
class Item:
    """One edit of the package, as written in its file."""

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


@dataclass(frozen=True)
class Package:
    """One package file: who asked for it, why, and its edits in order."""

    id: str
    author: str
    why: str
    items: tuple[Item, ...]


def parse(raw: object) -> Package:
    """Read one package of the feed.

    Raises ``PackageError`` naming the first problem: not an object, an
    unknown key or action, a missing field, or an id that is not
    ``<number>-<word>`` (the id is what ``tgedit.json`` remembers). A
    package is applied whole or not at all, so a typo stops it before
    anything is touched.
    """
    if not isinstance(raw, dict):
        msg = 'a package is a JSON object'
        raise PackageError(msg)
    name = _id(raw.get('id'))
    unknown = set(raw) - _PACKAGE_KEYS
    if unknown:
        msg = f'{name}: unknown key(s) {sorted(unknown)}'
        raise PackageError(msg)
    author, why = _head(raw, name)
    edits = raw.get('edits')
    if not isinstance(edits, list) or not edits:
        msg = f'{name}: edits must be a non-empty list'
        raise PackageError(msg)
    try:
        items = tuple(_item(n, one) for n, one in enumerate(edits, start=1))
    except PackageError as exc:
        msg = f'{name}: {exc}'
        raise PackageError(msg) from exc
    return Package(name, author, why, items)


def _id(value: object) -> str:
    """Return a package id, ``<number>-<word>``."""
    if not isinstance(value, str) or not _ID.fullmatch(value):
        msg = f'id {value!r} must be <number>-<word>'
        raise PackageError(msg)
    return value


def _head(raw: dict[str, object], name: str) -> tuple[str, str]:
    """Return the package's (author, why)."""
    author, why = raw.get('author'), raw.get('why')
    if not isinstance(author, str) or not author.strip():
        msg = f'{name}: author must name the role that asked for it'
        raise PackageError(msg)
    if not isinstance(why, str) or not why.strip() or '\n' in why:
        msg = f'{name}: why must be one line'
        raise PackageError(msg)
    return author.strip(), why.strip()


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


# ----------------------------------------------------------------- feed


FEED_VERSION = 1
"""The one feed layout this engine reads; any other is refused whole."""


class FeedError(ValueError):
    """The feed is refused whole; the message says why."""


@dataclass(frozen=True)
class Rules:
    """The feed's limits; ``tighten`` keeps the stricter of them and ours."""

    max_deletes: int = MAX_DELETES
    min_pause_sec: float = MIN_PAUSE_SEC

    def tighten(self, max_deletes: int, pause_sec: float) -> Rules:
        """Return the stricter of these rules and ``(max_deletes, pause)``.

        Never looser than the hard limits, whatever either side says.
        """
        return Rules(
            max_deletes=max(
                0, min(self.max_deletes, max_deletes, MAX_DELETES)
            ),
            min_pause_sec=max(self.min_pause_sec, pause_sec, MIN_PAUSE_SEC),
        )


@dataclass(frozen=True)
class Feed:
    """The feed: its limits and every package in it, in order."""

    rules: Rules
    packages: tuple[Package, ...]


def parse_feed(text: str) -> Feed:
    """Read the feed's text; raise ``FeedError`` on any problem.

    The site build checks the same schema, so a problem here means the
    two drifted apart: nothing is applied until a reviewed fix ships.
    """
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f'the feed does not parse: {exc.msg} at {exc.pos}'
        raise FeedError(msg) from exc
    if not isinstance(raw, dict):
        msg = 'the feed is not a JSON object'
        raise FeedError(msg)
    version = raw.get('version')
    if version != FEED_VERSION or isinstance(version, bool):
        msg = f'feed version {version!r}, this engine reads {FEED_VERSION}'
        raise FeedError(msg)
    packages = _packages(raw.get('packages'))
    return Feed(_rules(raw.get('rules', {})), packages)


def _packages(rows: object) -> tuple[Package, ...]:
    """Read the feed's packages; one bad package or a twice id refuses all."""
    if not isinstance(rows, list):
        msg = 'the feed has no packages list'
        raise FeedError(msg)
    try:
        packages = tuple(parse(one) for one in rows)
    except PackageError as exc:
        msg = f'the feed carries a broken package: {exc}'
        raise FeedError(msg) from exc
    ids = [one.id for one in packages]
    twice = sorted({one for one in ids if ids.count(one) > 1})
    if twice:
        msg = f'the feed carries id(s) {twice} more than once'
        raise FeedError(msg)
    return packages


def _rules(raw: object) -> Rules:
    """Read the feed's ``rules``; a missing limit keeps the hard one."""
    if not isinstance(raw, dict):
        msg = 'the feed rules are not an object'
        raise FeedError(msg)
    deletes = raw.get('max_deletes', MAX_DELETES)
    pause = raw.get('min_pause_sec', MIN_PAUSE_SEC)
    if not isinstance(deletes, int) or isinstance(deletes, bool):
        msg = f'feed rules: max_deletes {deletes!r} is not a whole number'
        raise FeedError(msg)
    if not isinstance(pause, (int, float)) or isinstance(pause, bool):
        msg = f'feed rules: min_pause_sec {pause!r} is not a number'
        raise FeedError(msg)
    return Rules(max_deletes=deletes, min_pause_sec=float(pause))


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
    ``labels`` maps the status and reason codes to the words the log
    shows; a code without a label is shown as is.
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
