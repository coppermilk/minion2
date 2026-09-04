# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""How a duration is written, which is one decision for the whole report."""

from __future__ import annotations

from minions.userbot.core.render import Glyphs
from minions.userbot.core.runtime import fmt_span

_MIN = 60
_HOUR = 60 * _MIN
_DAY = 24 * _HOUR


def test_the_span_is_the_coarsest_unit_that_fits() -> None:
    """One unit, never two -- the second never changed a decision.

    "1d 50s" and "1d 43m" are the same fact to anyone reading a roster, and
    carrying the second unit made the same number read three ways across one
    report: an age here, a countdown there, a window in the header.
    """
    assert fmt_span(0) == (0, 's')
    assert fmt_span(42) == (42, 's')
    assert fmt_span(3 * _MIN + 10) == (3, 'm')  # the 10s is dropped
    assert fmt_span(2 * _HOUR + 50 * _MIN) == (2, 'h')
    assert fmt_span(7 * _DAY + 4 * _HOUR) == (7, 'd')


def test_the_unit_changes_only_when_the_next_one_is_whole() -> None:
    """Truncating, not rounding: a countdown never claims to be shorter."""
    assert fmt_span(_MIN - 1) == (59, 's')
    assert fmt_span(_MIN) == (1, 'm')
    assert fmt_span(_HOUR - 1) == (59, 'm')
    assert fmt_span(_HOUR) == (1, 'h')
    assert fmt_span(_DAY - 1) == (23, 'h')
    assert fmt_span(_DAY) == (1, 'd')


def test_a_span_in_the_past_reads_as_none_of_it_left() -> None:
    """Negative seconds are an overdue countdown, not a negative number."""
    assert fmt_span(-500) == (0, 's')


def test_the_unit_is_a_key_not_a_letter() -> None:
    """Arithmetic in core, vocabulary in the JSON -- the split that matters.

    The module returns 'h'; the operator's letter is looked up beside it, so
    the same span renders in whatever language the constants file speaks
    without this function knowing there is one.
    """
    plain, russian = Glyphs(), Glyphs(units={'h': 'ch', 'd': 'd'})

    assert plain.span(2 * _HOUR) == '2h'  # no entry: the ASCII key stands
    assert russian.span(2 * _HOUR) == '2ch'
    assert russian.span(3 * _DAY) == '3d'
