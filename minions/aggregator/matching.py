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

from minions.aggregator.models import _THUMB_ALIASES
from minions.aggregator.models import Item
from minions.aggregator.models import _parse_iso

if TYPE_CHECKING:
    from collections.abc import Iterable

    from minions.aggregator.models import Consts
    from minions.aggregator.models import Group
    from minions.aggregator.models import Posted

_HASHTAG_RE = re.compile(r'#\S+')
_NONWORD_RE = re.compile(r'[^\w\s]')  # drops emoji and punctuation; keeps text


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
    """Similarity ratio of two normalized titles, in [0, 1]."""
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
