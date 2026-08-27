# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Human-mimicry primitives for CHOOSING (which), the peer of humanize_time.

A person picks with two biases a plain ``random.choice`` lacks: favourites
(some options are simply more likely) and anti-repetition (whatever was used
just now is suppressed, then fades back in). ``weighted_choice`` is the first;
``recency_penalty`` is a weight multiplier for the second. Both are pure
functions over an injected RNG, so any caller -- the cat brain choosing an
emoji, the post composer choosing an announce line -- can read like a person
instead of a uniform sampler, and stay deterministic under test.
"""

from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING
from typing import TypeVar

if TYPE_CHECKING:
    from collections.abc import Sequence

T = TypeVar('T')


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
