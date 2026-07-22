"""Cron-expression matching: is a moment due for a 5-field schedule.

Schedules are moderator settings (admin.json), so a bot fires on a cron
the operator edits from chat rather than a crontab line baked into the
image. This is a deliberate step past BLUEPRINT 11 (the wall clock lives
in cron only): the bot reads the clock to evaluate its own cron.

Fields are ``minute hour day-of-month month day-of-week`` (day-of-week
cron style, Sunday = 0). Each field is ``*``, a number, a ``a-b`` range, a
``*/n`` or ``a-b/n`` step, or a comma list of those. A malformed or blank
expression is never due.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

_FIELDS = 5
"""minute, hour, day-of-month, month, day-of-week."""


def cron_due(expr: str, now: datetime) -> bool:
    """Whether ``now`` matches a 5-field cron expression."""
    fields = expr.split()
    if len(fields) != _FIELDS:
        return False
    values = (
        now.minute,
        now.hour,
        now.day,
        now.month,
        now.isoweekday() % 7,  # cron day-of-week: Sunday = 0
    )
    return all(_match(f, v) for f, v in zip(fields, values, strict=True))


def _match(field: str, value: int) -> bool:
    """Whether one cron field (a comma list of parts) admits ``value``."""
    return any(_part(part, value) for part in field.split(','))


def _part(part: str, value: int) -> bool:
    """One cron part: ``*``, ``n``, ``a-b``, with an optional ``/step``."""
    base, step = _split_step(part)
    if base == '*':
        return value % step == 0
    lo, hi = _bounds(base)
    return lo <= value <= hi and (value - lo) % step == 0


def _split_step(part: str) -> tuple[str, int]:
    """Split ``base/step`` into its base and a positive step (default 1)."""
    base, sep, step = part.partition('/')
    return base, int(step) if sep and step.isdigit() and step != '0' else 1


def _bounds(base: str) -> tuple[int, int]:
    """The inclusive range a base spans; (1, 0) never matches anything."""
    lo, sep, hi = base.partition('-')
    if sep and lo.isdigit() and hi.isdigit():
        return int(lo), int(hi)
    if base.isdigit():
        return int(base), int(base)
    return (1, 0)
