# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Re-post guard: the time window OR the count window blocks a re-delivery.

The aggregator's ``main`` imports Telethon at module load, which the test
extras do not install, so we stub the handful of Telethon names it binds
before importing. Only the pure dedup helpers are exercised here.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

from tests.conftest import install_telethon_stub

if TYPE_CHECKING:
    import pytest

install_telethon_stub()

from minions.userbot import main  # noqa: E402
from minions.userbot.core.matching import is_recent_repost  # noqa: E402
from minions.userbot.core.models import Config  # noqa: E402
from minions.userbot.core.models import Group  # noqa: E402
from minions.userbot.core.models import Item  # noqa: E402
from minions.userbot.core.models import Posted  # noqa: E402
from minions.userbot.glue import aggregator  # noqa: E402


def _post(title: str, age_days: float) -> Posted:
    """Build a posted record ``age_days`` old (ISO time, second precision)."""
    at = datetime.fromtimestamp(
        time.time() - age_days * 86400, tz=UTC
    ).strftime('%Y-%m-%dT%H:%M:%SZ')
    return Posted(title=title, at=at, links={}, msg_ids=[])


WEEK = 604800.0


def test_time_window_blocks_recent_repost() -> None:
    """A title posted inside the time window is a re-post (count off)."""
    posted = [_post('Salsa dance', 1)]
    assert is_recent_repost(
        posted,
        'Salsa dance',
        time.time(),
        threshold=0.9,
        window=WEEK,
        count=0,
    )


def test_count_window_catches_what_time_misses() -> None:
    """A stale title (older than the time window) is still blocked by count.

    This is the "every day it comes back" failure: the source keeps
    re-delivering a video long after the time window expired. The
    clock-independent count window catches it while it is among the last N.
    """
    posted = [
        _post('Salsa dance', 10),  # older than a week: time window misses it
        _post('Cooking pasta', 2),
        _post('Gym fail', 1),
    ]
    assert not is_recent_repost(
        posted,
        'Salsa dance',
        time.time(),
        threshold=0.9,
        window=WEEK,
        count=0,
    )
    assert is_recent_repost(
        posted,
        'Salsa dance',
        time.time(),
        threshold=0.9,
        window=WEEK,
        count=3,
    )


def test_eligible_again_beyond_both_windows() -> None:
    """Once past the time AND the count window, the title may post again."""
    posted = [
        _post('Salsa dance', 10),
        _post('a', 9),
        _post('b', 8),
        _post('c', 7),  # push it beyond count 3
    ]
    assert not is_recent_repost(
        posted,
        'Salsa dance',
        time.time(),
        threshold=0.9,
        window=WEEK,
        count=3,
    )


def test_time_still_blocks_beyond_count() -> None:
    """A recent title beyond the count window is still caught by time."""
    posted = [
        _post('X recent', 0.04),  # ~1h old: inside the week
        _post('a', 0),
        _post('b', 0),
        _post('c', 0),
        _post('d', 0),
    ]
    assert is_recent_repost(
        posted,
        'X recent',
        time.time(),
        threshold=0.9,
        window=WEEK,
        count=3,
    )


def test_both_windows_off_disables_guard() -> None:
    """With both knobs at 0 the guard never fires."""
    posted = [_post('Salsa dance', 0)]
    assert not is_recent_repost(
        posted,
        'Salsa dance',
        time.time(),
        threshold=0.9,
        window=0,
        count=0,
    )


def test_fuzzy_match_ignores_hashtag_and_emoji_tail() -> None:
    """Same wording, different hashtag/emoji tail, still a re-post."""
    posted = [_post('Three days editing this number and finally done', 0)]
    variant = 'Three days editing this number and finally done #banger #fun'
    assert is_recent_repost(
        posted,
        variant,
        time.time(),
        threshold=0.9,
        window=0,
        count=3,
    )


class _FakeFlush:
    """Stand in for a flush's I/O and record the order side effects run in."""

    def __init__(self, delivered: list[tuple[int, int]]) -> None:
        self.order: list[str] = []
        self._delivered = delivered

    async def deliver(self, *_: object) -> list[tuple[int, int]]:
        self.order.append('deliver')
        return list(self._delivered)

    async def react(self, *_: object) -> None:
        self.order.append('react')

    async def watch(self, *_: object) -> None:
        self.order.append('watch')

    def save(self) -> None:
        self.order.append('save')

    def arm(self, *_: object) -> None:
        self.order.append('arm')


def _bare_aggregator(fake: _FakeFlush) -> main.Userbot:
    """Build an Userbot with only what the flush path touches (no __init__).

    ``__init__`` opens a Telethon client and loads real state, so we make the
    instance directly and wire in the fake collaborators.
    """
    agg = object.__new__(main.Userbot)
    agg.groups = []
    agg.posted = []
    agg.processed_ids = set()
    agg.consts = None  # only compose reads it, and we patch compose
    agg._variety = None  # passed to the patched compose, which ignores it
    agg.config = Config(
        source=0,
        targets=(),
        test_target=0,
        platforms=('tiktok', 'youtube', 'pinterest', 'instagram'),
        threshold=0.9,
        timeout=10800.0,
        backfill=100,
        max_duration=180,
        repost_guard=604800.0,
        repost_guard_count=5,
        discussion_gap=0.0,
    )
    agg._deliver_post = fake.deliver
    agg._react_to_post = fake.react
    agg._watch_post = fake.watch
    agg._save = fake.save
    agg._arm = fake.arm

    def _record_posted(group: Group) -> None:
        fake.order.append('record')
        main.Userbot._record_posted(agg, group)

    agg._record_posted = _record_posted
    return agg


def _sample_group() -> Group:
    """Return a two-platform group like the one that looped in the wild."""
    items = {
        'pinterest': Item(
            key='pinterest',
            platform='pinterest',
            title='V',
            url='https://pin/1',
            thumbnail='',
            duration='',
            msg_id=539,
        ),
        'youtube': Item(
            key='youtube',
            platform='youtube',
            title='V',
            url='https://yt/1',
            thumbnail='',
            duration='',
            msg_id=546,
        ),
    }
    return Group(title='V', items=items, msg_ids={539, 546})


def _patch_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub message composition so no Telethon types are needed."""
    monkeypatch.setattr(aggregator, 'compose', lambda *a, **k: object())
    monkeypatch.setattr(aggregator, 'youtube_thumb', lambda group: '')


def test_flush_records_and_saves_before_react_watch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delivered post is recorded and persisted before the flaky steps.

    This is the fix for the re-post loop: the old order recorded AFTER
    react/watch, so a failure or restart in those steps left the post
    unrecorded and re-posted it on the next start.
    """
    _patch_compose(monkeypatch)
    fake = _FakeFlush(delivered=[(1, 100)])
    agg = _bare_aggregator(fake)
    group = _sample_group()
    agg.groups.append(group)

    asyncio.run(main.Userbot._flush(agg, group))

    assert fake.order.index('record') < fake.order.index('react')
    assert fake.order.index('save') < fake.order.index('react')
    assert fake.order.index('save') < fake.order.index('watch')
    assert group not in agg.groups
    assert len(agg.posted) == 1
    # The source ids are now marked processed, so a backfill re-scan skips
    # them -- the mechanism that stops the daily re-post.
    assert agg.processed_ids == {539, 546}


def test_flush_requeues_when_nothing_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post that does not go out is re-queued, not dropped or recorded."""
    _patch_compose(monkeypatch)
    fake = _FakeFlush(delivered=[])
    agg = _bare_aggregator(fake)
    group = _sample_group()
    agg.groups.append(group)

    asyncio.run(main.Userbot._flush(agg, group))

    assert group in agg.groups  # kept for a later retry
    assert agg.posted == []  # not recorded
    assert 'record' not in fake.order
    assert 'arm' in fake.order  # re-armed
    assert 'save' in fake.order  # the re-queue is persisted


def _agg_with_gap(gap: float) -> main.Userbot:
    """Build a bare Userbot whose config sets only the discussion gap."""
    agg = object.__new__(main.Userbot)
    agg.config = Config(
        source=0,
        targets=(),
        test_target=0,
        platforms=('tiktok',),
        threshold=0.9,
        timeout=1.0,
        backfill=0,
        max_duration=180,
        repost_guard=0.0,
        repost_guard_count=0,
        discussion_gap=gap,
    )
    agg._last_discussion_ts = 0.0
    return agg


def test_discussion_throttle_spaces_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call soon after the previous one waits out the remaining gap."""
    slept: list[float] = []

    async def _fake_sleep(sec: float) -> None:
        slept.append(sec)

    monkeypatch.setattr(main.asyncio, 'sleep', _fake_sleep)
    gap = 2.0
    agg = _agg_with_gap(gap)
    agg._last_discussion_ts = time.time()  # a call just happened

    asyncio.run(main.Userbot._throttle_discussion(agg))

    assert slept
    assert 0 < slept[0] <= gap


def test_discussion_throttle_disabled_when_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero gap disables the throttle -- no sleep at all."""
    slept: list[float] = []

    async def _fake_sleep(sec: float) -> None:
        slept.append(sec)

    monkeypatch.setattr(main.asyncio, 'sleep', _fake_sleep)
    agg = _agg_with_gap(0.0)

    asyncio.run(main.Userbot._throttle_discussion(agg))

    assert slept == []
