# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The shared human-mimicry primitives (minions/userbot/humanize_time.py).

Pure functions used by both behavioural brains (cats, stories); these pin the
properties the brains rely on: local-time reading, a positive heavy-tailed
delay, a quiet-hours gate, and a deterministic-per-date silent-day roll.
"""

from __future__ import annotations

import itertools
import random
from datetime import UTC
from datetime import datetime

from minions.userbot.core import humanize_choice
from minions.userbot.core import humanize_time

_NOON = datetime(1970, 1, 1, 12, 0, tzinfo=UTC).timestamp()
_OFFSET = 3


def test_local_applies_the_offset() -> None:
    """Check local applies the offset."""
    assert humanize_time.local(_NOON, 0.0).hour == 12  # noqa: PLR2004 -- midday
    assert humanize_time.local(_NOON, 3.0).hour == 12 + _OFFSET


def test_lognormal_is_positive() -> None:
    """Check lognormal is positive."""
    rng = random.Random(0)
    assert all(humanize_time.lognormal(rng, 1.0, 0.5) > 0 for _ in range(100))


def test_in_quiet_hours() -> None:
    """Check in quiet hours."""
    assert humanize_time.in_quiet_hours(_NOON, 0.0, frozenset({12}))
    assert not humanize_time.in_quiet_hours(_NOON, 0.0, frozenset({3}))


def test_silent_day_is_deterministic_and_bounded() -> None:
    """Check silent day is deterministic and bounded."""
    assert not humanize_time.is_silent_day(_NOON, 0.0, 0.0)  # disabled
    assert humanize_time.is_silent_day(_NOON, 0.0, 1.0)  # always
    # Same date -> same verdict on repeat calls (seeded by the date string).
    first = humanize_time.is_silent_day(_NOON, 0.0, 0.5)
    second = humanize_time.is_silent_day(_NOON, 0.0, 0.5)
    assert first == second


def test_recency_penalty_suppresses_then_recovers() -> None:
    """0 right after use, rising toward 1; <= 0 half-life disables it."""
    assert humanize_choice.recency_penalty(0.0, 100.0) == 0.0
    mid = humanize_choice.recency_penalty(100.0, 100.0)
    assert 0.0 < mid < 1.0
    assert humanize_choice.recency_penalty(1e9, 100.0) > 0.99  # noqa: PLR2004
    assert humanize_choice.recency_penalty(5.0, 0.0) == 1.0  # disabled


def test_weighted_choice_respects_weights() -> None:
    """A zero-weight item is never picked; all-zero falls back to uniform."""
    rng = random.Random(0)
    items = ('a', 'b', 'c')
    picks = {
        humanize_choice.weighted_choice(rng, items, [1.0, 0.0, 1.0])
        for _ in range(200)
    }
    assert picks == {'a', 'c'}  # 'b' (weight 0) never chosen
    only = humanize_choice.weighted_choice(rng, items, [0.0, 0.0, 0.0])
    assert only in items  # all-zero -> a valid uniform pick


def test_variety_avoids_back_to_back_repeats() -> None:
    """A pool of two never repeats; a single-item pool always returns it."""
    v = humanize_choice.Variety(random.Random(0))
    picks = [v.pick('k', ('a', 'b')) for _ in range(10)]
    assert all(a != b for a, b in itertools.pairwise(picks))
    assert v.pick('solo', ('only',)) == 'only'
