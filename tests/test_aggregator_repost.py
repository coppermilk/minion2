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
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest


from minions.userbot.core.matching import is_recent_repost
from minions.userbot.core.models import Config
from minions.userbot.core.models import Group
from minions.userbot.core.models import Item
from minions.userbot.core.models import Posted
from minions.userbot.engines import premium_emoji
from minions.userbot.glue import aggregator


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

    async def on_posted(self, *_: object) -> None:
        self.order.append('watch')

    def save(self) -> None:
        self.order.append('save')

    def arm(self, *_: object) -> None:
        self.order.append('arm')


def _bare_aggregator(fake: _FakeFlush) -> aggregator.LinkAggregator:
    """Build a poster with only what the flush path touches.

    No object.__new__ and no Telethon: the poster is an object now, so the
    test constructs one and swaps in the fakes it wants to observe.
    """
    agg = aggregator.LinkAggregator(
        aggregator.AggregatorDeps(
            account=None,
            config=Config(
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
            ),
            consts=None,  # only compose reads it, and we patch compose
            state_path=None,  # _save is faked
            targets=tuple,
            on_posted=fake.on_posted,
            field_keys=(),
            variety=None,  # the patched compose ignores it
        )
    )
    agg._deliver_post = fake.deliver
    agg._save = fake.save
    agg._arm = fake.arm

    def _record_posted(group: Group) -> None:
        fake.order.append('record')
        aggregator.LinkAggregator._record_posted(agg, group)

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

    asyncio.run(agg._flush(group))

    assert fake.order.index('record') < fake.order.index('watch')
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

    asyncio.run(agg._flush(group))

    assert group in agg.groups  # kept for a later retry
    assert agg.posted == []  # not recorded
    assert 'record' not in fake.order
    assert 'arm' in fake.order  # re-armed
    assert 'save' in fake.order  # the re-queue is persisted


class _FakeAccount:
    """An Account that reports what went out, and what it was asked to send."""

    def __init__(self, *, photo_id: int = 0, text_id: int = 0) -> None:
        """Answer send_photo with ``photo_id`` and send with ``text_id``."""
        self.photo_id = photo_id
        self.text_id = text_id
        self.calls: list[str] = []

    async def send_photo(self, chat: int, photo: str, text: object) -> int:
        """Record the photo attempt and answer with the canned id."""
        self.calls.append(f'photo:{chat}:{photo}')
        return self.photo_id

    async def send(self, chat: int, text: object) -> int:
        """Record the text attempt and answer with the canned id."""
        self.calls.append(f'text:{chat}')
        return self.text_id


def _delivering(account: _FakeAccount) -> aggregator.LinkAggregator:
    """Return a poster wired to ``account``, with two targets."""
    agg = _bare_aggregator(_FakeFlush([]))
    # _bare_aggregator stubs delivery out; here delivery IS the subject.
    del agg._deliver_post
    agg.deps = replace(agg.deps, account=account, targets=lambda: (11, 22))
    return agg


def _deliver(agg: aggregator.LinkAggregator, thumb: str) -> object:
    """Run one delivery of a trivial message with ``thumb``."""
    message = premium_emoji.PremiumMessage('body')
    return asyncio.run(agg._deliver_post(message, thumb))


def test_a_thumbnail_post_never_also_sends_the_text() -> None:
    """A photo that lands is the whole post -- no second, text copy."""
    account = _FakeAccount(photo_id=500)
    assert _deliver(_delivering(account), 'https://img/1.jpg') == [
        (11, 500),
        (22, 500),
    ]
    assert account.calls == [
        'photo:11:https://img/1.jpg',
        'photo:22:https://img/1.jpg',
    ]


def test_a_refused_thumbnail_falls_back_to_text() -> None:
    """The adapter answers 0 rather than raising, so 0 IS the fallback."""
    account = _FakeAccount(photo_id=0, text_id=700)
    assert _deliver(_delivering(account), 'https://img/bad.jpg') == [
        (11, 700),
        (22, 700),
    ]
    assert account.calls[:2] == [
        'photo:11:https://img/bad.jpg',
        'text:11',
    ]


def test_nothing_delivered_means_nothing_recorded() -> None:
    """An empty result is what tells the caller to re-queue the group.

    The old code reached this by catching an exception per target; the
    adapter degrades instead, so the check is now "did it come back with
    an id" -- and a post recorded under id 0 would be a post the reaction
    engine then watches at message 0.
    """
    account = _FakeAccount(photo_id=0, text_id=0)
    assert _deliver(_delivering(account), '') == []
    assert account.calls == ['text:11', 'text:22']
