# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Human-mimicry primitives shared by the behavioural engines.

Two halves of the same kit, used by the reaction and story engines (and the
post composer) so they read like a person, not a scheduler:

* WHEN a person acts -- the persona's local time, heavy-tailed positive delays,
  a quiet-hours gate, and a deterministic-per-date "silent day" roll.
* WHICH thing a person picks -- favourites (``weighted_choice``) and
  anti-repetition (``recency_penalty`` weight, and ``Variety`` for no
  back-to-back repeats).

Everything is a pure function over primitives (a timezone offset, a
probability, an injected RNG) -- no Telethon, no state -- so it is trivially
unit-testable and callable from any params object.
"""

from __future__ import annotations

import math
import random
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import TYPE_CHECKING
from typing import TypeVar

if TYPE_CHECKING:
    from collections.abc import Sequence

T = TypeVar('T')


# --- WHEN a person acts -------------------------------------------------


def local(ts: float, tz_offset_hours: float) -> datetime:
    """Return ``ts`` as a datetime in the persona's timezone.

    Hours and dates are read in THIS timezone, so an engine's cadence matches
    the persona's day, not the server's UTC clock.
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


# --- WHICH thing a person picks -----------------------------------------


def recency_penalty(dt: float, half_life: float) -> float:
    """Weight multiplier for an item last used ``dt`` seconds ago.

    0 right after use (fully suppressed), recovering to 1 over ``half_life``
    -- this is what kills both back-to-back repeats and unnatural uniformity.
    ``half_life`` <= 0 disables it (always 1).
    """
    if half_life <= 0:
        return 1.0
    return 1.0 - math.exp(-dt / half_life)


def weighted_choice(
    rng: random.Random, items: Sequence[T], weights: Sequence[float]
) -> T:
    """Pick one item proportional to its weight (uniform if all are zero)."""
    total = sum(weights)
    if total <= 0:
        return rng.choice(items)
    threshold = rng.random() * total
    upto = 0.0
    for item, weight in zip(items, weights, strict=True):
        upto += weight
        if upto >= threshold:
            return item
    return items[-1]


class Variety:
    """Pick from small pools like a person: no back-to-back repeats.

    A person does not use the identical announce line or lead emoji on two
    posts in a row, which is exactly what a plain ``random.choice`` does now
    and then. This wraps an RNG and a per-pool memory of the last pick, and
    suppresses that pick next time (uniform among the rest). In-memory only --
    cosmetic variety, so a restart may repeat once, which does not matter.
    """

    def __init__(self, rng: random.Random | None = None) -> None:
        """Use ``rng`` (default a fresh one) and start with no history."""
        self._rng = rng or random.Random()  # noqa: S311 -- variety, not crypto
        self._last: dict[str, object] = {}

    def pick(self, key: str, items: Sequence[T]) -> T:
        """Return an item from ``items``, avoiding the last one for ``key``."""
        if len(items) <= 1:
            return items[0]
        last = self._last.get(key)
        weights = [0.0 if item == last else 1.0 for item in items]
        chosen = weighted_choice(self._rng, items, weights)
        self._last[key] = chosen
        return chosen
