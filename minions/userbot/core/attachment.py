# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Berlyne attachment model: how much a viewer warms to the account.

Pure, stdlib-only. Four factors multiply into one index, using the ORIGINAL
functional forms (not a polynomial fitted to the same peak):

- ``exposure(p)`` -- the Wundt/Berlyne curve: a logistic reward system minus a
  logistic aversion (overexposure) system, normalized to its own max. This is
  the genuine inverted-U, not ``p**2 * (1 - p)``. Viewing too little leaves a
  stranger; viewing everything (p -> 1) falls down the aversion side (reads as
  stalking). ``exposure_peak`` returns the p that maximizes it -- the view
  target the story engine steers each peer toward.
- ``recip(r)`` -- Reis & Shaver reciprocity: a saturating (negatively
  accelerated) response to how often a view is answered with a reaction.
- ``variety(v)`` -- Whitchurch (uncertainty raises attraction); linear.
- ``mass_pen(c)`` -- a penalty for clearing everything in one burst; linear.

``p`` = fraction of a peer's stories viewed, ``v`` = timing irregularity,
``r`` = fraction of views answered with a reaction, ``c`` = burst clumping.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_PEAK_GRID = 1000  # resolution of the argmax search over p in [0, 1]
_RECIP_RATE = 3.0  # saturation rate of the reciprocity curve
_VARIETY_GAIN = 0.6  # attraction bonus at maximum timing irregularity
_MASS_PENALTY = 0.5  # attraction lost when every view lands in one burst


@dataclass(frozen=True)
class WundtParams:
    """Shape of the Wundt exposure curve.

    Reward turns on at ``c1``, aversion (overexposure) at ``c2`` > ``c1``, both
    with slope ``k``; ``w`` weights the aversion arm. Defaults peak near
    two-thirds exposure -- view most of a person's stories, but not all.
    """

    c1: float = 0.45
    c2: float = 0.90
    k: float = 8.0
    w: float = 1.0


_DEFAULT = WundtParams()


def _sigma(x: float) -> float:
    """Logistic (sigmoid) function."""
    return 1.0 / (1.0 + math.exp(-x))


def _wundt_raw(p: float, params: WundtParams = _DEFAULT) -> float:
    """Unnormalized Wundt tone: reward sigmoid minus aversion sigmoid."""
    reward = _sigma(params.k * (p - params.c1))
    aversion = _sigma(params.k * (p - params.c2))
    return reward - params.w * aversion


def exposure_peak(params: WundtParams = _DEFAULT) -> float:
    """Return the p in [0, 1] that maximizes the Wundt curve (view target)."""
    grid = (i / _PEAK_GRID for i in range(_PEAK_GRID + 1))
    return max(grid, key=lambda p: _wundt_raw(p, params))


def exposure(p: float, params: WundtParams = _DEFAULT) -> float:
    """Return the Wundt exposure at ``p``, normalized to its peak (1.0)."""
    peak = _wundt_raw(exposure_peak(params), params)
    if peak <= 0.0:
        return 0.0
    return _wundt_raw(p, params) / peak


def recip(r: float) -> float:
    """Reciprocity factor: saturating response to the reaction rate ``r``."""
    return 1.0 - math.exp(-_RECIP_RATE * r)


def variety(v: float) -> float:
    """Variety factor: a bonus for irregular (surprising) view timing."""
    return 1.0 + _VARIETY_GAIN * v


def mass_pen(c: float) -> float:
    """Mass penalty: attraction lost when views clump into one burst."""
    return 1.0 - _MASS_PENALTY * c


def attachment_index(  # noqa: PLR0913 -- the four model factors read best flat
    p: float, v: float, r: float, c: float
) -> float:
    """Combine the four factors into one attachment index (>= 0)."""
    return exposure(p) * variety(v) * recip(r) * mass_pen(c)
