# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Value types and small time helpers shared across the aggregator.

Extracted from ``main`` so the dataclasses (Config, Item, Group, Posted,
Consts) and the timestamp helpers live in one dependency-free place that the
other aggregator modules can import without pulling in Telethon.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio


def iso(ts: float) -> str:
    """Return a unix timestamp as an ISO-8601 UTC string (second precision)."""
    return datetime.fromtimestamp(ts, tz=UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_iso(text: str) -> float:
    """Return an ISO-8601 UTC string to a unix timestamp (0 on bad)."""
    try:
        return datetime.fromisoformat(text).timestamp()
    except (ValueError, TypeError):
        return time.time()


def story_epoch(value: object) -> float:
    """Return a story item's date as a unix timestamp, 0 if unknown.

    Telethon gives ``StoryItem.date`` as a ``datetime`` (not an epoch int), so
    freshest-first ordering must convert it; a raw number is accepted too, and
    anything unparseable degrades to 0 (ordering falls back, never crashes).
    """
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        return float(value or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


# The incoming-message JSON keys, so a typo in the API can be fixed in the
# constants file (the "fields" object) without touching code.
DEFAULT_FIELDS = {
    'action': 'action',
    'caption': 'caption',
    'platform': 'platform',
    'link': 'link',
    'thumbnail': 'thumnailUrl',
    'duration': 'duration',
}
# Thumbnail key spellings seen in the wild; any is accepted (optional field).
THUMB_ALIASES = ('thumbnail', 'thumbnailUrl', 'thumnailUrl')


@dataclass(frozen=True)
class Config:
    """Runtime settings for the aggregator, all resolved from the env."""

    source: int
    targets: tuple[int, ...]
    test_target: int  # TEST_CHAT_ID: where posts go in test mode (0 = unset)
    platforms: tuple[str, ...]
    threshold: float
    timeout: float
    backfill: int
    max_duration: int
    # Do not re-post a video whose title is >= threshold similar to a recent
    # post. Guards against the source re-delivering the same video (new message
    # ids -- e.g. an upstream re-emit after the chat's auto-delete clears the
    # old ones), which the per-message-id guard cannot catch. Two windows, and
    # a match in EITHER one blocks the re-post:
    #   repost_guard      -- time window in seconds (0 disables this window).
    #   repost_guard_count -- how many of the most recent posted videos to
    #                         guard against, regardless of time (0 disables).
    # The count window is clock-independent, so it holds through restarts and
    # source floods that the time window can miss.
    repost_guard: float
    repost_guard_count: int
    # Minimum seconds between consecutive GetDiscussionMessageRequest calls
    # (resolving a post's comment thread). They fire in bursts -- the last
    # watch_posts posts per target on every startup and rescan -- and Telegram
    # flood-limits them, so we space them out process-wide. 0 disables.
    discussion_gap: float


@dataclass(frozen=True)
class Item:
    """One platform's message about a video."""

    key: str  # normalized platform, e.g. 'youtube'
    platform: str  # display name as received
    title: str
    url: str
    thumbnail: str
    duration: str
    msg_id: int


@dataclass
class Group:
    """A set of platform items believed to be the same video."""

    title: str
    items: dict[str, Item] = field(default_factory=dict)
    msg_ids: set[int] = field(default_factory=set)
    created_at: float = field(default_factory=time.time)
    task: asyncio.Task[None] | None = None


@dataclass(frozen=True)
class Posted:
    """A readable record of one video that was posted (state log + dedup)."""

    title: str
    at: str  # ISO 8601 UTC, e.g. '2026-07-23T14:20:00Z'
    links: dict[str, str]  # platform -> url
    msg_ids: list[int]  # the source messages consumed by this post


@dataclass(frozen=True)
class Comment:
    """A comment to maybe reaction: chat, thread root, message id and text."""

    chat: int
    root: int
    msg_id: int
    text: str = ''  # a snippet of what the commenter wrote (for /status)


@dataclass(frozen=True)
class Consts:
    """Randomizable texts and emoji for the post, loaded from JSON."""

    fields: dict[str, str]
    action_value: str
    author: str
    announce: list[str]
    love: list[object]
    lead: list[object]  # random premium emoji that leads the caption line
    arrow_down: list[object]
    view_label: list[str]
    column_separator: str
    rows: list[list[str]]
    platform_emoji: dict[str, object]
    sample_short: str
    sample_long: str
    status_help: str  # the /status legend (expected behaviour), from JSON
    help_text: str  # the /help menu (plain-language command list), from JSON
    help_hint: str  # nudge shown for an unknown /command, from JSON
    # Substrings that mark a comment as wanting a real reply (business/outreach
    # terms + any non-ASCII marks): a sticker is suppressed there, a plain
    # reaction goes instead. Non-ASCII, so it lives in the JSON, not here.
    human_words: tuple[str, ...]
    # /status icons (emoji, so JSON not source):
    # title/routing/videos/reactions/
    # greeter/users/legend section glyphs + on/off dots + bullet/arrow.
    status: dict[str, str]
    emoji_all: list[
        dict[str, object]
    ]  # unified emoji catalog (new JSON), else []
