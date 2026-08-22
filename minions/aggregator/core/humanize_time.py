# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Shared human-mimicry primitives used by the behavioural brains.

Both ``cats.py`` (who to react to, when) and ``stories.py`` (whose stories to
view, when) need the same small kit to read like a person and not a scheduler:
the persona's local time, heavy-tailed positive delays, a quiet-hours gate, and
a deterministic-per-date "silent day" roll. Those live here, once, so the two
brains share one implementation instead of each carrying its own copy.

Everything is a pure function over primitives (a timezone offset, a
probability, an injected RNG) -- no dataclass, no Telethon, no state -- so it
is trivially unit-testable and callable from any params object.
"""

from __future__ import annotations

import math
import random
from datetime import datetime
from datetime import timedelta
from datetime import timezone


def local(ts: float, tz_offset_hours: float) -> datetime:
    """Return ``ts`` as a datetime in the persona's timezone.

    Hours and dates are read in THIS timezone, so a brain's cadence matches the
    persona's day, not the server's UTC clock.
    """
    tz = timezone(timedelta(hours=tz_offset_hours))
    return datetime.fromtimestamp(ts, tz=tz)


def lognormal(rng: random.Random, mu: float, sigma: float) -> float:
    """Return a heavy-tailed positive draw: exp of a normal.

    A human's gaps are not uniform -- most are short, a few are very long. The
    log-normal is that shape, so it models both the quick back-to-back action
    and the occasional long silence with one pair of knobs.
    """
    return math.exp(rng.gauss(mu, sigma))


def in_quiet_hours(
    ts: float, tz_offset_hours: float, quiet_hours: frozenset[int]
) -> bool:
    """Return whether ``ts``'s local hour is a quiet (asleep) hour."""
    return local(ts, tz_offset_hours).hour in quiet_hours


def is_silent_day(ts: float, tz_offset_hours: float, prob: float) -> bool:
    """Return whether the whole day at ``ts`` is a silent one.

    Deterministic per date (seeded by the date string) so a restart does not
    flip a day that was already decided: a day the persona simply did not show
    up. ``prob`` <= 0 disables it.
    """
    if prob <= 0:
        return False
    day = local(ts, tz_offset_hours).strftime('%Y-%m-%d')
    roll = random.Random(day).random()  # noqa: S311 -- mimicry, not crypto
    return roll < prob
