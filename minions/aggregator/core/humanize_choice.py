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
from typing import TYPE_CHECKING
from typing import TypeVar

if TYPE_CHECKING:
    import random
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
