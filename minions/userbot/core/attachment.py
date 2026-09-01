# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Berlyne attachment model: how much a viewer warms to the account.

Pure, stdlib-only. Four measured factors multiply into one index, each keeping
the ORIGINAL functional form of the effect it stands for -- not a polynomial
fitted to the same peak. Every one of them is somebody's published finding,
so the citation lives here rather than in a commit message:

- ``exposure(p)`` -- the Wundt/Berlyne curve: a logistic reward system minus a
  logistic aversion (overexposure) system, normalized to its own max. Zajonc
  (1968) showed bare repeated contact raises liking; Bornstein's meta-analysis
  (1989, 208 studies, r ~ 0.26) found the effect real but inverted-U, and
  Berlyne (1970) gave the shape its mechanism -- habituation competing with
  tedium. So this is the genuine inverted-U, not ``p**2 * (1 - p)``: engaging
  too little leaves a stranger, engaging everything (p -> 1) falls down the
  aversion side and reads as stalking. ``exposure_peak`` returns the p that
  maximizes it -- the target both engines steer each peer toward.
- ``recip(r)`` -- reciprocity, saturating (negatively accelerated). Reis &
  Shaver (1988) put intimacy in the cycle "disclosure -> perceived partner
  responsiveness", not in attention as such; Altman & Taylor (1973) describe
  the deepening as layered, along breadth of topic and depth of disclosure.
- ``variety(v)`` -- irregular timing, linear. Whitchurch, Wilson & Gilbert
  (2011, Psych. Science) found uncertainty about another's interest raised
  attraction ABOVE certainty of strong interest; Ferster & Skinner (1957)
  established that variable schedules of reinforcement are the ones most
  resistant to extinction.
- ``mass_pen(c)`` -- a penalty for clearing everything in one burst, linear.
  Same Bornstein/Berlyne pairing: massed repetition reaches tedium sooner than
  the same number of contacts spread out. (Ebbinghaus 1885 is the familiar
  spacing result, but that is MEMORY -- carried here as an analogy, not as
  evidence about liking.)

``p`` = fraction of a peer's offers we engaged, ``v`` = irregularity of our
timing toward them, ``r`` = fraction of engagements answered with the stronger
act, ``c`` = how much of our attention arrives in bursts.

Where the numbers come from: ``p`` and ``r`` are ratios of plain ledger
counters. ``v`` and ``c`` are derived in ``core/relationship.py`` from the
per-peer gap statistics ``core/state.py`` accumulates -- so all four are
measured, none assumed. One knob has NO study behind it and should not be
mistaken for one: the weight given to a like versus a written reply is an
extrapolation of Reis/Shaver and Altman/Taylor ("a like carries zero
disclosure"), not a measured effect.
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


@dataclass(frozen=True)
class Factors:
    """The four measured inputs of the attachment index, for one peer.

    A value object rather than four positional floats: at a call site
    ``Factors(p, v, r, c)`` cannot silently swap two of them, and the index
    keeps room for the Wundt params without growing a parameter list.
    """

    p: float  # engaged / offered -- exposure
    v: float  # irregularity of our timing toward them -- variety
    r: float  # reciprocated / engaged -- reciprocity
    c: float  # share of engagements arriving in a burst -- mass penalty


def attachment_index(f: Factors, params: WundtParams = _DEFAULT) -> float:
    """Combine the four factors into one attachment index (>= 0).

    Bounded by [0, 1.6): ``exposure``, ``recip`` and ``mass_pen`` are each at
    most 1, and ``variety`` reaches 1.6 at maximum irregularity.
    """
    return (
        exposure(f.p, params) * variety(f.v) * recip(f.r) * mass_pen(f.c)
    )
