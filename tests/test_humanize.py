# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The shared human-mimicry primitives (minions/userbot/humanize.py).

Pure functions used by both behavioural brains (cats, stories); these pin the
properties the brains rely on: local-time reading, a positive heavy-tailed
delay, a quiet-hours gate, and a deterministic-per-date silent-day roll.
"""

from __future__ import annotations

import itertools
import random
from datetime import UTC
from datetime import datetime

from minions.userbot.core import humanize

_NOON = datetime(1970, 1, 1, 12, 0, tzinfo=UTC).timestamp()
_OFFSET = 3


def test_local_applies_the_offset() -> None:
    """Check local applies the offset."""
    assert humanize.local(_NOON, 0.0).hour == 12  # noqa: PLR2004 -- midday
    assert humanize.local(_NOON, 3.0).hour == 12 + _OFFSET


def test_lognormal_is_positive() -> None:
    """Check lognormal is positive."""
    rng = random.Random(0)
    assert all(humanize.lognormal(rng, 1.0, 0.5) > 0 for _ in range(100))


def test_in_quiet_hours() -> None:
    """Check in quiet hours."""
    assert humanize.in_quiet_hours(_NOON, 0.0, frozenset({12}))
    assert not humanize.in_quiet_hours(_NOON, 0.0, frozenset({3}))


def test_silent_day_is_deterministic_and_bounded() -> None:
    """Check silent day is deterministic and bounded."""
    assert not humanize.is_silent_day(_NOON, 0.0, 0.0)  # disabled
    assert humanize.is_silent_day(_NOON, 0.0, 1.0)  # always
    # Same date -> same verdict on repeat calls (seeded by the date string).
    first = humanize.is_silent_day(_NOON, 0.0, 0.5)
    second = humanize.is_silent_day(_NOON, 0.0, 0.5)
    assert first == second


def test_recency_penalty_suppresses_then_recovers() -> None:
    """0 right after use, rising toward 1; <= 0 half-life disables it."""
    assert humanize.recency_penalty(0.0, 100.0) == 0.0
    mid = humanize.recency_penalty(100.0, 100.0)
    assert 0.0 < mid < 1.0
    assert humanize.recency_penalty(1e9, 100.0) > 0.99  # noqa: PLR2004
    assert humanize.recency_penalty(5.0, 0.0) == 1.0  # disabled


def test_weighted_choice_respects_weights() -> None:
    """A zero-weight item is never picked; all-zero falls back to uniform."""
    rng = random.Random(0)
    items = ('a', 'b', 'c')
    picks = {
        humanize.weighted_choice(rng, items, [1.0, 0.0, 1.0])
        for _ in range(200)
    }
    assert picks == {'a', 'c'}  # 'b' (weight 0) never chosen
    only = humanize.weighted_choice(rng, items, [0.0, 0.0, 0.0])
    assert only in items  # all-zero -> a valid uniform pick


def test_variety_avoids_back_to_back_repeats() -> None:
    """A pool of two never repeats; a single-item pool always returns it."""
    v = humanize.Variety(random.Random(0))
    picks = [v.pick('k', ('a', 'b')) for _ in range(10)]
    assert all(a != b for a, b in itertools.pairwise(picks))
    assert v.pick('solo', ('only',)) == 'only'


class _Marks:
    """An in-memory stand-in for the StateStore dedup marks."""

    def __init__(self) -> None:
        self.keys: set[str] = set()

    def mark(self, key: str) -> bool:
        new = key not in self.keys
        self.keys.add(key)
        return new

    def marked(self, key: str) -> bool:
        return key in self.keys

    def drop_marks(self, prefix: str) -> None:
        self.keys = {k for k in self.keys if not k.startswith(prefix)}


_POOL = ('a', 'b', 'c', 'd', 'e')


def test_phrase_bag_uses_the_whole_pool_before_any_repeat() -> None:
    """Each round of len(pool) draws holds every phrase exactly once."""
    bag = humanize.PhraseBag(_Marks(), 'p:', random.Random(1))
    first = [bag.draw(_POOL) for _ in _POOL]
    assert sorted(first) == sorted(_POOL)


def test_phrase_bag_does_not_repeat_across_the_round_boundary() -> None:
    """The phrase that closed a round never opens the next one."""
    for seed in range(50):
        bag = humanize.PhraseBag(_Marks(), 'p:', random.Random(seed))
        draws = [bag.draw(_POOL) for _ in range(40)]
        assert all(x != y for x, y in itertools.pairwise(draws))


def test_phrase_bag_memory_survives_a_new_bag() -> None:
    """A restart builds a new bag over the same marks and keeps the round."""
    marks = _Marks()
    used = {humanize.PhraseBag(marks, 'p:').draw(_POOL) for _ in range(3)}
    rest = {humanize.PhraseBag(marks, 'p:').draw(_POOL) for _ in range(2)}
    assert used | rest == set(_POOL)


def test_phrase_bag_takes_new_phrases_and_ignores_empty() -> None:
    """An edited pool needs no migration; empty lines are never drawn."""
    marks = _Marks()
    bag = humanize.PhraseBag(marks, 'p:', random.Random(0))
    for _ in range(4):
        bag.draw(('a', 'b', 'c', 'd', 'e'))
    assert bag.draw(('a', 'b', 'c', 'd', 'e', 'f', 'g')) in {'e', 'f', 'g'}
    assert bag.draw(('', '')) == ''
    assert bag.draw(('only',)) == 'only'
    assert bag.draw(('only',)) == 'only'


def test_phrase_bag_keeps_its_marks_in_the_state_db(tmp_path) -> None:  # noqa: ANN001
    """The real store: marks persist across Database instances."""
    from minions.userbot.core.state import DB_NAME
    from minions.userbot.core.state import Database

    def bag() -> humanize.PhraseBag:
        return humanize.PhraseBag(
            Database(tmp_path / DB_NAME).store('aggregator'), 'announce:'
        )

    used = {bag().draw(_POOL) for _ in range(4)}
    assert {bag().draw(_POOL)} == set(_POOL) - used
