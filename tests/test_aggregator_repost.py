# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Re-post guard: the time window OR the count window blocks a re-delivery.

The aggregator's ``main`` imports Telethon at module load, which the test
extras do not install, so we stub the handful of Telethon names it binds
before importing. Only the pure dedup helpers are exercised here.
"""

from __future__ import annotations

import sys
import time
import types
from datetime import UTC
from datetime import datetime


class _AnyMeta(type):
    """A class whose every attribute is another such class (nested access)."""

    def __getattr__(cls, name: str) -> type:
        return _AnyMeta(name, (), {})


def _stub_module(name: str) -> types.ModuleType:
    """Return a module that fabricates any imported name as a dummy class."""
    module = types.ModuleType(name)

    def _fabricate(attr: str) -> type:
        return _AnyMeta(attr, (), {})

    module.__getattr__ = _fabricate  # type: ignore[method-assign]
    module.__path__ = []  # mark it as a package
    return module


def _stub_telethon() -> None:
    """Register fake Telethon modules so ``main`` imports without it.

    Telethon is a runtime-only dependency (extra ``tg``), absent from the
    test extras. Each stub module answers any ``from telethon... import X``
    with a throwaway class, so ``main`` and its siblings load unchanged.
    """
    if 'telethon' in sys.modules:
        return
    for name in (
        'telethon',
        'telethon.events',
        'telethon.utils',
        'telethon.tl',
        'telethon.tl.functions',
        'telethon.tl.functions.messages',
        'telethon.tl.functions.stories',
        'telethon.tl.types',
    ):
        sys.modules[name] = _stub_module(name)


_stub_telethon()

from minions.aggregator import main  # noqa: E402


def _post(title: str, age_days: float) -> main.Posted:
    """Build a posted record ``age_days`` old (ISO time, second precision)."""
    at = datetime.fromtimestamp(
        time.time() - age_days * 86400, tz=UTC
    ).strftime('%Y-%m-%dT%H:%M:%SZ')
    return main.Posted(title=title, at=at, links={}, msg_ids=[])


WEEK = 604800.0


def test_time_window_blocks_recent_repost() -> None:
    """A title posted inside the time window is a re-post (count off)."""
    posted = [_post('Salsa dance', 1)]
    assert main._is_recent_repost(
        posted, 'Salsa dance', time.time(),
        threshold=0.9, window=WEEK, count=0,
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
    assert not main._is_recent_repost(
        posted, 'Salsa dance', time.time(),
        threshold=0.9, window=WEEK, count=0,
    )
    assert main._is_recent_repost(
        posted, 'Salsa dance', time.time(),
        threshold=0.9, window=WEEK, count=3,
    )


def test_eligible_again_beyond_both_windows() -> None:
    """Once past the time AND the count window, the title may post again."""
    posted = [
        _post('Salsa dance', 10),
        _post('a', 9), _post('b', 8), _post('c', 7),  # push it beyond count 3
    ]
    assert not main._is_recent_repost(
        posted, 'Salsa dance', time.time(),
        threshold=0.9, window=WEEK, count=3,
    )


def test_time_still_blocks_beyond_count() -> None:
    """A recent title beyond the count window is still caught by time."""
    posted = [
        _post('X recent', 0.04),  # ~1h old: inside the week
        _post('a', 0), _post('b', 0), _post('c', 0), _post('d', 0),
    ]
    assert main._is_recent_repost(
        posted, 'X recent', time.time(),
        threshold=0.9, window=WEEK, count=3,
    )


def test_both_windows_off_disables_guard() -> None:
    """With both knobs at 0 the guard never fires."""
    posted = [_post('Salsa dance', 0)]
    assert not main._is_recent_repost(
        posted, 'Salsa dance', time.time(),
        threshold=0.9, window=0, count=0,
    )


def test_fuzzy_match_ignores_hashtag_and_emoji_tail() -> None:
    """Same wording, different hashtag/emoji tail, still a re-post."""
    posted = [_post('Three days editing this number and finally done', 0)]
    variant = 'Three days editing this number and finally done #banger #fun'
    assert main._is_recent_repost(
        posted, variant, time.time(),
        threshold=0.9, window=0, count=3,
    )
