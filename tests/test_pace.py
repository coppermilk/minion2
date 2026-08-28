# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The outbound turnstile: spacing, ceilings, and the FloodWait feedback.

Driven on a fake clock, so the properties are asserted rather than waited
for. The concurrency test is the point of the module: the hand-rolled
throttles it replaces read a timestamp, slept, then wrote it, which lets two
coroutines entering together fire at the same instant.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
from itertools import pairwise

import pytest

from minion_core.pace import FLOOD_WIDEN
from minion_core.pace import WINDOW_SEC
from minion_core.pace import Gate
from minion_core.pace import Pace

GAP = 2.0
BURST = 8
CEILING = 5
TWO = 2


class _Clock:
    """A clock that only moves when something sleeps on it."""

    def __init__(self) -> None:
        """Start at zero with nothing elapsed."""
        self.now = 0.0

    def __call__(self) -> float:
        """Return the current fake time."""
        return self.now

    async def sleep(self, seconds: float) -> None:
        """Advance to the wake-up time, letting other tasks run first."""
        await asyncio.sleep(0)
        self.now = max(self.now, self.now + seconds)


@dataclass
class _Bench:
    """A gate wired to a fake clock, plus the stamps it let through."""

    gate: Gate
    clock: _Clock = field(default_factory=_Clock)

    def __post_init__(self) -> None:
        """Point the gate at the fake clock."""
        self.gate.clock = self.clock
        self.gate.sleep = self.clock.sleep

    def run(self, kind: str, n: int = 2) -> list[float]:
        """Run ``n`` requests of one kind; return when each was let through."""

        async def go() -> list[float]:
            out = []
            for _ in range(n):
                await self.gate.wait(kind)
                out.append(self.clock.now)
            return out

        return asyncio.run(go())

    def burst(self, kind: str, n: int) -> list[float]:
        """Run ``n`` requests CONCURRENTLY; return their stamps, sorted."""

        async def go() -> list[float]:
            seen: list[float] = []

            async def one() -> None:
                await self.gate.wait(kind)
                seen.append(self.clock.now)

            await asyncio.gather(*(one() for _ in range(n)))
            return seen

        return sorted(asyncio.run(go()))


def _bench(**paces: Pace) -> _Bench:
    """Build a bench whose gate limits the named kinds."""
    return _Bench(Gate(dict(paces)))


def _gaps(stamps: list[float]) -> list[float]:
    """Return the interval between each consecutive pair of stamps."""
    return [b - a for a, b in pairwise(stamps)]


def test_min_gap_spaces_consecutive_requests() -> None:
    """No two requests of a kind land closer than its floor."""
    gaps = _gaps(_bench(read=Pace(min_gap_sec=GAP)).run('read', 4))
    assert all(g >= GAP for g in gaps), gaps


def test_a_kind_with_no_pace_is_not_delayed() -> None:
    """An unlisted kind costs nothing -- Pace() means no limit."""
    assert _bench().run('read', 5) == [0.0] * 5


def test_per_minute_is_a_ceiling_over_the_window() -> None:
    """The rolling ceiling holds: the N+1st request waits out the window."""
    stamps = _bench(read=Pace(per_minute=CEILING)).run('read', CEILING + 1)
    assert stamps[:CEILING] == [0.0] * CEILING  # the budget goes at once
    assert stamps[CEILING] >= WINDOW_SEC  # the next one waits it out


def test_a_slow_kind_does_not_hold_up_a_fast_one() -> None:
    """A DM waiting its 45 seconds must not delay a read."""
    bench = _bench(dm=Pace(min_gap_sec=45.0), read=Pace())

    async def go() -> float:
        await bench.gate.wait('dm')
        await bench.gate.wait('read')  # no dm gap applies to this
        return bench.clock.now

    assert asyncio.run(go()) == 0.0


def test_the_overall_pace_applies_across_kinds() -> None:
    """Telegram counts per account, so one ceiling sits above the kinds."""
    bench = _Bench(Gate({}, overall=Pace(min_gap_sec=GAP)))

    async def go() -> list[float]:
        out = []
        for kind in ('read', 'write', 'react'):
            await bench.gate.wait(kind)
            out.append(bench.clock.now)
        return out

    gaps = _gaps(asyncio.run(go()))
    assert all(g >= GAP for g in gaps), gaps


def test_concurrent_callers_get_separate_slots() -> None:
    """The property the throttles this replaces got wrong.

    Read-sleep-write lets two coroutines entering together read the same
    stale mark, sleep the same amount and fire at the same instant. Here the
    slot is reserved under a lock, so a burst of callers is spread.
    """
    stamps = _bench(read=Pace(min_gap_sec=GAP)).burst('read', BURST)
    assert len(set(stamps)) == BURST, f'collided: {stamps}'
    assert all(g >= GAP for g in _gaps(stamps)), _gaps(stamps)


def test_flood_widens_the_spacing_it_was_raised_on() -> None:
    """A FloodWait is feedback, not just a nap: the same run comes out slower.

    Compared against an identical gate that was never flooded, which is the
    honest statement -- the multiplier also decays as requests succeed, so
    an exact factor would be pinning the decay rate, not the behaviour.
    """
    calm = _bench(react=Pace(min_gap_sec=GAP)).run('react', TWO)
    flooded = _bench(react=Pace(min_gap_sec=GAP))
    flooded.gate.flooded('react')
    assert _gaps(flooded.run('react', TWO))[0] > _gaps(calm)[0]


def test_flood_widens_the_account_not_just_the_kind() -> None:
    """One kind tripping a limit slows the whole account down."""
    bench = _bench(react=Pace(min_gap_sec=GAP))
    bench.gate.flooded('react')
    assert bench.gate.slack('') > 1.0


def test_widening_decays_back_toward_nominal() -> None:
    """One bad patch must not slow the account down for good."""
    bench = _bench(read=Pace())
    bench.gate.flooded('read')
    widened = bench.gate.slack('read')
    bench.run('read', 50)
    assert 1.0 <= bench.gate.slack('read') < widened


@pytest.mark.parametrize('kind', ['read', 'write', 'dm'])
def test_slack_is_never_below_nominal(kind: str) -> None:
    """Decay stops at 1.0; the gate never runs faster than configured."""
    bench = _bench(**{kind: Pace()})
    bench.run(kind, 200)
    assert bench.gate.slack(kind) == 1.0


# ------------------------------------------------------- reading the gate


def test_free_in_reserves_nothing() -> None:
    """Reading the gate must not spend the slots it is describing.

    /status renders every lane on each call, so if free_in booked a slot
    the report would throttle the bot -- and the more often an operator
    looked at it, the slower the account would get.
    """
    bench = _bench(read=Pace(min_gap_sec=GAP))
    before = [bench.gate.free_in('read') for _ in range(5)]
    assert before == [0.0] * 5  # idle lane, and still idle after 5 reads
    assert bench.run('read', 2) == [0.0, GAP]  # unchanged by the reading


def test_free_in_reports_the_wait_a_request_would_face() -> None:
    """After a request, the lane says how long the next one must wait."""
    bench = _bench(read=Pace(min_gap_sec=GAP))
    bench.run('read', 1)
    assert bench.gate.free_in('read') == GAP


def test_free_in_includes_the_overall_pace() -> None:
    """A lane with no pace of its own still answers the account ceiling."""
    bench = _Bench(Gate({}, overall=Pace(min_gap_sec=GAP)))
    bench.run('read', 1)
    assert bench.gate.free_in('story') == GAP


def test_lanes_report_every_paced_kind_and_its_widening() -> None:
    """A flooded lane shows up widened, which is the FloodWait made visible."""
    bench = _bench(read=Pace(min_gap_sec=GAP), dm=Pace(min_gap_sec=GAP))
    bench.gate.flooded('dm')
    lanes = {lane.kind: lane for lane in bench.gate.lanes()}
    assert sorted(lanes) == ['dm', 'read']
    assert lanes['dm'].slack == FLOOD_WIDEN
    assert lanes['read'].slack == 1.0
