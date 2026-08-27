# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The Berlyne attachment model (minions/userbot/engines/attachment.py).

Pins the original functional forms: the Wundt exposure curve (a difference of
two logistics, NOT p^2(1-p)), its ~0.675 peak, the saturating reciprocity
response, and the qualitative scenario ranking the model is meant to produce.
"""

from __future__ import annotations

from minions.userbot.core import attachment

_PEAK = 0.675
_PEAK_TOL = 0.02
_TWO_THIRDS = 0.667


def test_exposure_peaks_near_two_thirds() -> None:
    """The Wundt argmax is ~0.675 -- the view target, not viewing all."""
    assert abs(attachment.exposure_peak() - _PEAK) <= _PEAK_TOL
    assert abs(attachment.exposure(_PEAK) - 1.0) <= _PEAK_TOL  # peak = 1


def test_exposure_falls_off_past_the_peak() -> None:
    """Viewing everything (p=1) sits down the aversion side, below the peak."""
    assert attachment.exposure(1.0) < attachment.exposure(_TWO_THIRDS)
    assert attachment.exposure(0.1) < attachment.exposure(_TWO_THIRDS)
    # It is genuinely the difference of two sigmoids, not the cubic p^2(1-p):
    # the cubic at p=1 is exactly 0, the Wundt tail is clearly positive.
    assert attachment.exposure(1.0) > 0.2  # noqa: PLR2004


def test_recip_is_zero_without_reactions_and_saturates() -> None:
    """No reaction -> no reciprocity; it rises and saturates with r."""
    assert attachment.recip(0.0) == 0.0
    mid = attachment.recip(0.20)
    assert 0.4 < mid < 0.5  # noqa: PLR2004 -- 1 - e^-0.6 ~ 0.45
    assert attachment.recip(1.0) > attachment.recip(0.5)  # still rising
    assert attachment.recip(1.0) < 1.0  # never quite reaches 1


def test_scenario_ranking_matches_the_model() -> None:
    """Once/day beats once/month beats 8x/day (the model's whole point)."""
    once_month = attachment.attachment_index(p=0.95, v=0.50, r=0.75, c=0.0)
    once_day = attachment.attachment_index(p=0.70, v=0.80, r=0.25, c=0.0)
    eight_day = attachment.attachment_index(p=0.40, v=0.60, r=0.125, c=0.40)
    assert once_day > once_month > eight_day
