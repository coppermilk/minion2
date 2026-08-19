# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The shared human-mimicry primitives (minions/aggregator/humanize.py).

Pure functions used by both behavioural brains (cats, stories); these pin the
properties the brains rely on: local-time reading, a positive heavy-tailed
delay, a quiet-hours gate, and a deterministic-per-date silent-day roll.
"""

from __future__ import annotations

import random
from datetime import UTC
from datetime import datetime

from minions.aggregator import humanize

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
