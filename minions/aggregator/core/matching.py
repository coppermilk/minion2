# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Pure title matching, dedup and incoming-message parsing helpers.

Extracted from ``main``: fuzzy title normalisation/comparison, the re-post
guard predicate, the loose JSON field extractor and the Item builder. All
stateless, so they are unit-testable without a live client.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

from minions.aggregator.core.models import _THUMB_ALIASES
from minions.aggregator.core.models import Item
from minions.aggregator.core.models import _parse_iso

if TYPE_CHECKING:
    from collections.abc import Iterable

    from minions.aggregator.core.models import Consts
    from minions.aggregator.core.models import Group
    from minions.aggregator.core.models import Posted

_HASHTAG_RE = re.compile(r'#\S+')
_NONWORD_RE = re.compile(r'[^\w\s]')  # drops emoji and punctuation; keeps text
# A prefix shorter than this is too generic to trust as a same-video signal.
_MIN_PREFIX_CHARS = 12


def _norm(title: str) -> str:
    """Caption core for fuzzy matching: no hashtags, emoji, or punctuation.

    The same video carries different hashtag/emoji tails per platform, so we
    compare only the wording. Falls back to the raw text if stripping empties
    it (a caption that is nothing but hashtags/emoji).
    """
    text = _NONWORD_RE.sub(' ', _HASHTAG_RE.sub(' ', title))
    core = ' '.join(text.lower().split())
    return core or ' '.join(title.lower().split())


def _similar(a: str, b: str) -> float:
    """Similarity of two normalized titles, in [0, 1] (prefix-aware).

    The same video often carries a longer caption on one platform -- a second
    sentence, or hashtags stripped unevenly -- so the shorter caption is a
    clean PREFIX of the longer. ``SequenceMatcher.ratio`` falls off with the
    length gap (a prefix can score ~0.6), which splits one video into two
    half-collected groups that then wait forever for each other's platforms.
    A long-enough exact prefix is a strong same-video signal, so treat it as a
    full match; otherwise fall back to the plain ratio.
    """
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= _MIN_PREFIX_CHARS and long.startswith(short):
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _is_recent_repost(  # noqa: PLR0913 -- a small pure predicate, flat reads best
    posted: list[Posted],
    title: str,
    now: float,
    *,
    threshold: float,
    window: float,
    count: int,
) -> bool:
    """Whether ``title`` matches a recently posted video (time OR count).

    A pure helper (no self) so it is unit-testable: compares the normalized
    title against each recent ``Posted`` record's title by the same fuzzy
    ratio the in-flight dedup uses. A record counts as recent when it is
    within ``window`` seconds (a time guard, <= 0 disables it) OR among the
    last ``count`` posted videos (a clock-independent guard, <= 0 disables
    it). A match in either window blocks the re-post. With both disabled the
    guard is off. Records are oldest-first, so the reverse walk stops once a
    record is outside both windows -- every earlier one is older still.
    """
    if window <= 0 and count <= 0:
        return False
    norm = _norm(title)
    for idx, post in enumerate(reversed(posted)):  # newest first
        within_count = idx < count
        within_window = window > 0 and now - _parse_iso(post.at) <= window
        if not within_count and not within_window:
            break  # beyond both guards; the rest are older and further back
        if _similar(norm, _norm(post.title)) >= threshold:
            return True
    return False


def _duration_seconds(text: str) -> int:
    """Parse 'H:M:S' / 'M:S' / 'S' to seconds; -1 if unknown or unparseable."""
    text = text.strip()
    if not text:
        return -1
    try:
        parts = [int(p) for p in text.split(':')]
    except ValueError:
        return -1
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def _action_ok(data: dict[str, object], consts: Consts) -> bool:
    """Whether the message's action is the one we act on (or no filter set)."""
    if not consts.action_value:
        return True
    value = str(data.get(consts.fields['action']) or '')
    return value == consts.action_value


def _extract_fields(text: str, keys: Iterable[str]) -> dict[str, str]:
    """Pull "key": value pairs from possibly-invalid JSON-ish text.

    The source API is not strict JSON (trailing commas, unquoted or unclosed
    values), so instead of json.loads we find each wanted key and read its
    value: a quoted string, or a bareword up to the next comma or brace.
    """
    value_re = r'"\s*:\s*("(?:[^"\\]|\\.)*"|[^,}\n]*)'
    found: dict[str, str] = {}
    for key in keys:
        match = re.search('"' + re.escape(key) + value_re, text)
        if match is None:
            continue
        value = match.group(1).strip().removeprefix('"').removesuffix('"')
        found[key] = value.replace('\\/', '/').replace('\\"', '"').strip()
    return found


def _parse_item(
    data: dict[str, object], msg_id: int, fields: dict[str, str]
) -> Item | None:
    """Build an Item from a parsed JSON object, or None if incomplete.

    ``fields`` maps our names to the incoming JSON keys, so a renamed or
    misspelled API key is fixed in the constants file, not here.
    """
    title = str(data.get(fields['caption']) or '').strip()
    platform = str(data.get(fields['platform']) or '').strip()
    if not title or not platform:
        return None
    return Item(
        key=platform.lower(),
        platform=platform,
        title=title,
        url=str(data.get(fields['link']) or '').strip(),
        thumbnail=_pick(data, fields['thumbnail'], *_THUMB_ALIASES),
        duration=str(data.get(fields['duration']) or '').strip(),
        msg_id=msg_id,
    )


def _pick(data: dict[str, object], *keys: str) -> str:
    """First non-empty value among ``keys`` (handles optional/renamed keys)."""
    for key in keys:
        value = str(data.get(key) or '').strip()
        if value:
            return value
    return ''


def _primary(group: Group, order: Iterable[str]) -> Item:
    """Return the highest-priority item present; its caption/thumbnail lead."""
    for key in order:
        item = group.items.get(key)
        if item is not None:
            return item
    return next(iter(group.items.values()))


# Link markers that mean a comment wants a real reply (ASCII, so inline);
# the business/outreach words and any non-ASCII marks (e.g. a full-width '?')
# live in the constants JSON's "human_words" (BLUEPRINT 4: source stays ASCII).
_LINK_MARKERS = ('http://', 'https://', 't.me/', 'www.')


def _needs_human(text: str, words: tuple[str, ...]) -> bool:
    """Whether a comment wants a real reply, not an auto sticker.

    A question, a link, or business/outreach wording is exactly where a canned
    cat STICKER (a message-shaped reply) reads as a non-sequitur. The caller
    uses this to downgrade such comments to a plain REACTION (safe on anything)
    instead. It never blocks the reaction -- it only keeps message-stickers off
    comments a human would actually answer. This is the one light content gate;
    the engine is otherwise content-agnostic. ``words`` (from the JSON) carries
    the business terms and any non-ASCII marks.
    """
    low = text.lower()
    if '?' in text:
        return True
    if any(u in low for u in _LINK_MARKERS):
        return True
    return any(w in low for w in words)


def _thread_top(reply: object) -> int | None:
    """Return the thread-root id a reply belongs to (comment target), or None.

    A comment on a channel post is a reply in the discussion group: its
    ``reply_to_top_id`` is the post's thread root; a first-level comment has
    only ``reply_to_msg_id`` (the same root). Either way this yields the id the
    engine watches, so nested and top-level comments both map to their post.
    """
    top = getattr(reply, 'reply_to_top_id', None)
    if top is not None:
        return int(top)
    msg = getattr(reply, 'reply_to_msg_id', None)
    return int(msg) if msg is not None else None
