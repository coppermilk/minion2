"""Cron-expression matching for moderator-editable schedules."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

from minion_core.adapters.schedule import cron_due


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 7, 20, hour, minute, tzinfo=UTC)


def test_minute_hour_and_wildcards() -> None:
    """A plain min/hour cron matches only that moment; * is always due."""
    now = _at(8, 0)
    assert cron_due('0 8 * * *', now)
    assert not cron_due('0 9 * * *', now)  # wrong hour
    assert not cron_due('30 8 * * *', now)  # wrong minute
    assert cron_due('* * * * *', now)  # every minute


def test_day_of_week_cron_style() -> None:
    """Day-of-week is cron style (Sunday = 0), taken from the date."""
    now = _at(8, 0)
    dow = now.isoweekday() % 7
    assert cron_due(f'0 8 * * {dow}', now)
    assert not cron_due(f'0 8 * * {(dow + 1) % 7}', now)


def test_lists_ranges_and_steps() -> None:
    """Comma lists, a-b ranges and */n steps all match."""
    now = _at(8, 0)
    assert cron_due('0 8,20 * * *', now)  # list
    assert cron_due('0 6-9 * * *', now)  # range covers hour 8
    assert cron_due('0 */4 * * *', now)  # 8 is divisible by 4
    assert not cron_due('0 */5 * * *', now)  # 8 is not divisible by 5


def test_malformed_expression_is_never_due() -> None:
    """A blank or wrong-arity expression matches nothing."""
    now = _at(8, 0)
    assert not cron_due('', now)
    assert not cron_due('0 8 * *', now)  # four fields
    assert not cron_due('nonsense text here now', now)
