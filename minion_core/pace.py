# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The turnstile every outbound request passes: how often, and how fast.

A userbot's only real risk is being told to slow down and not noticing. The
Telethon setting that looks like a limiter -- ``flood_sleep_threshold`` --
is nothing of the sort: it reacts AFTER Telegram answers FloodWait, and it
sleeps the one coroutine that asked. Every other task keeps firing, so the
account has been warned and the process has not.

``Gate`` is the preventive half. A caller waits BEFORE the request, and the
wait is computed under a lock so two coroutines entering together get two
different slots -- the hand-rolled throttles this replaces read a timestamp,
slept, then wrote it, which lets both callers read the same stale mark and
fire at once.

Requests are grouped by KIND (reads, writes, reactions, stories, DMs, the
liveness probe) because a limit that suits a read is far too loose for a DM,
and there is one overall pace above them because Telegram counts per
account, not per method.

Telegram publishes no rate table for user accounts, so fixed numbers are
either needlessly slow or useless. ``flooded`` closes the loop: a FloodWait
widens that kind's spacing and the overall spacing, and both decay back as
requests keep succeeding. Stdlib-only and clock-injected, so the whole thing
is testable without a network.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from collections.abc import Mapping

WINDOW_SEC = 60.0
"""The rolling window ``per_minute`` is counted over."""

FLOOD_WIDEN = 2.0
"""How much a FloodWait multiplies the offending kind's spacing."""

MAX_SLACK = 16.0
"""Ceiling on that multiplier, so one bad hour cannot wedge the bot."""

RECOVER = 0.98
"""Per-request decay of the multiplier back toward 1.0."""


@dataclass(frozen=True)
class Pace:
    """How often one kind of request may be made.

    ``min_gap_sec`` is the floor between two consecutive requests of the
    kind; ``per_minute`` is the ceiling over a rolling minute. 0 disables
    either half, so ``Pace()`` is "no limit" and costs nothing.
    """

    min_gap_sec: float = 0.0
    per_minute: int = 0


@dataclass
class Gate:
    """Serialise outbound requests to a pace, per kind and overall.

    ``clock`` and ``sleep`` are injected so tests drive it without waiting;
    production uses ``time.monotonic`` (never the wall clock -- a clock step
    must not release a burst).
    """

    paces: Mapping[str, Pace]
    overall: Pace = Pace()
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    _at: dict[str, float] = field(default_factory=dict)
    _seen: dict[str, deque[float]] = field(default_factory=dict)
    _slack: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Create the lock that guards every reservation."""
        self._lock = asyncio.Lock()

    async def wait(self, kind: str) -> None:
        """Block until a request of ``kind`` may go out.

        The slot is reserved under the lock and slept for outside it, so
        the gate never serialises two kinds against each other -- a DM
        waiting its 45 seconds must not hold up a read.
        """
        async with self._lock:
            at = self._reserve(kind)
        delay = at - self.clock()
        if delay > 0:
            await self.sleep(delay)

    def flooded(self, kind: str) -> None:
        """Telegram asked us to slow down: widen this kind and the whole.

        Called by the adapter when a request comes back with FloodWait. The
        widening decays on its own as later requests succeed, so a single
        bad patch does not slow the account down for good.
        """
        for name in (kind, ''):
            widened = self.slack(name) * FLOOD_WIDEN
            self._slack[name] = min(widened, MAX_SLACK)

    def slack(self, kind: str) -> float:
        """Return the multiplier in force for ``kind`` (1.0 = nominal)."""
        return self._slack.get(kind, 1.0)

    def _reserve(self, kind: str) -> float:
        """Claim the next free moment for ``kind``; return when it is."""
        now = self.clock()
        at = max(
            now,
            self._free(kind, self.paces.get(kind, Pace()), now),
            self._free('', self.overall, now),
        )
        self._record(kind, at)
        self._record('', at)
        self._relax(kind)
        return at

    def _free(self, kind: str, pace: Pace, now: float) -> float:
        """Return the earliest moment ``pace`` allows for ``kind``.

        A kind with no previous request is not held: the gap is measured
        FROM the last one, and there is none. (An absent mark read as 0.0
        would delay the very first request by a whole gap.)
        """
        slack = self.slack(kind)
        last = self._at.get(kind)
        earliest = now if last is None else last + pace.min_gap_sec * slack
        seen = self._seen.get(kind)
        if pace.per_minute > 0 and seen and len(seen) >= pace.per_minute:
            earliest = max(earliest, seen[0] + WINDOW_SEC * slack)
        return max(earliest, now)

    def _record(self, kind: str, at: float) -> None:
        """Book a request of ``kind`` at ``at`` and drop what fell out."""
        self._at[kind] = at
        seen = self._seen.setdefault(kind, deque())
        seen.append(at)
        while seen and seen[0] <= at - WINDOW_SEC:
            seen.popleft()

    def _relax(self, kind: str) -> None:
        """Ease the widening back toward nominal after a clean request."""
        for name in (kind, ''):
            slack = self._slack.get(name)
            if slack is not None and slack > 1.0:
                self._slack[name] = max(1.0, slack * RECOVER)
