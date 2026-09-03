# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The shared per-peer relationship control (engines/relationship.py).

The story and like engines both drive this: one memory (``Ledger``) and one
control law (``steer``). These pin the shared kernel independently of either
engine's plan/commit wiring.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from minions.userbot.core import attachment
from minions.userbot.core import relationship
from minions.userbot.core.state import DB_NAME
from minions.userbot.core.state import Database

if TYPE_CHECKING:
    from pathlib import Path

_TZ = 0.0
_NOON = 12 * 3600.0  # a fixed local timestamp (tz 0)
_NEXT_DAY = _NOON + 86400.0
_TARGET = 0.20
_TOL = 0.05
_STEPS = 3000
_CAP = 4
_CONTROL = relationship.Control(wundt=attachment.WundtParams())


def _ids(n: int) -> tuple[int, ...]:
    """Return ``n`` distinct subjects -- the things an act was about.

    The ledger counts what it is given rather than a bare number now, so a
    test that means "three offers" says which three.
    """
    return tuple(range(1, n + 1))


def _ledger(tmp_path: Path) -> relationship.Ledger:
    """Return a ledger over a fresh store (the counters live in SQLite)."""
    store = Database(tmp_path / DB_NAME).store('reactions')
    return relationship.Ledger(store)


def _steer_only() -> relationship.Control:
    """Build a control with no daily caps, to isolate the steering law."""
    return relationship.Control(wundt=attachment.WundtParams())


def test_steer_pushes_below_target_up_and_above_target_down(
    tmp_path: Path,
) -> None:
    """The P-controller corrects toward the target from either side."""
    assert relationship.steer(0.0, _TARGET, 1.0) > _TARGET
    assert relationship.steer(0.5, _TARGET, 1.0) < _TARGET
    assert relationship.steer(_TARGET, _TARGET, 1.0) == _TARGET


def test_steer_clamps_to_a_probability() -> None:
    """A large correction never leaves the [0, 1] range."""
    assert relationship.steer(1.0, _TARGET, 5.0) == 0.0
    assert relationship.steer(0.0, 0.9, 5.0) == 1.0


def test_take_prob_converges_offered_taken_to_the_wundt_peak(
    tmp_path: Path,
) -> None:
    """Repeated take decisions drive taken/offered onto the exposure peak."""
    led = _ledger(tmp_path)
    ctrl = _steer_only()
    rng = random.Random(0)
    peer = 1
    for _ in range(_STEPS):
        if rng.random() < led.take_prob(peer, ctrl):
            led.add_take(peer, _ids(1), _CONTROL, _NOON)
        else:
            led.add_offer(peer, _ids(1))
    ratio = led.row(peer).taken / led.row(peer).offered
    assert abs(ratio - ctrl.take_target()) < _TOL


def test_recip_prob_converges_recip_taken_to_the_target(
    tmp_path: Path,
) -> None:
    """Repeated recip decisions drive recip/taken onto recip_target."""
    led = _ledger(tmp_path)
    ctrl = _steer_only()
    rng = random.Random(1)
    peer = 1
    for _ in range(_STEPS):
        led.add_take(peer, _ids(1), _CONTROL, _NOON)  # a taken exposure
        if rng.random() < led.recip_prob(peer, ctrl, taken_now=True):
            led.bump_recip(peer)
    ratio = led.row(peer).recip / led.row(peer).taken
    assert abs(ratio - _TARGET) < _TOL


def test_spend_take_and_recip_clamp_at_the_daily_cap(tmp_path: Path) -> None:
    """Daily budgets stop at the cap and roll over at local midnight."""
    led = _ledger(tmp_path)
    ctrl = relationship.Control(
        wundt=attachment.WundtParams(), take_cap=_CAP, recip_cap=_CAP
    )
    taken = sum(led.spend_take(ctrl, _NOON, _TZ) for _ in range(100))
    recipped = sum(led.spend_recip(ctrl, _NOON, _TZ) for _ in range(100))
    assert taken == _CAP
    assert recipped == _CAP
    assert led.takes_today(_NOON, _TZ) == _CAP
    # Next local day: the counters reset, so the cap is available again.
    assert led.spend_take(ctrl, _NEXT_DAY, _TZ) is True
    assert led.takes_today(_NEXT_DAY, _TZ) == 1


def test_recip_left_reads_without_consuming(tmp_path: Path) -> None:
    """The story engine's plan-time budget read does not spend a slot."""
    led = _ledger(tmp_path)
    ctrl = relationship.Control(wundt=attachment.WundtParams(), recip_cap=_CAP)
    assert led.recip_left(ctrl, _NOON, _TZ) == _CAP
    led.add_recip(1, _ids(1), _NOON, _TZ)
    assert led.recip_left(ctrl, _NOON, _TZ) == _CAP - 1
    assert led.recip_left(ctrl, _NOON, _TZ) == _CAP - 1  # still, read-only


def test_warmth_orders_by_recency_and_evict_drops_a_peer(
    tmp_path: Path,
) -> None:
    """warmth() lists the most recent peer first; evict removes a peer."""
    early, late = 111, 222
    led = _ledger(tmp_path)
    led.add_take(early, _ids(2), _CONTROL, _NOON)
    led.add_recip(early, _ids(1), _NOON, _TZ)
    led.add_offer(late, _ids(3))  # interacted with more recently
    rows = relationship.warmth(led, _CONTROL)
    assert [row.peer_id for row in rows] == [late, early]  # newest first
    led.add_offer(early, _ids(1))  # touching them again moves them up
    assert next(r.peer_id for r in relationship.warmth(led, _CONTROL)) == early
    led.evict(early)
    assert led.row(early).offered == 0


def test_a_readout_row_names_a_peer_by_id_and_nothing_else(
    tmp_path: Path,
) -> None:
    """The ledger keeps numbers; who a peer IS lives once, in ``actors``.

    A row used to carry a display label as well, so the same person's name
    travelled beside every number about them -- and a peer who changed their
    @name read as two different people down the readout.
    """
    led = _ledger(tmp_path)
    led.add_take(552, _ids(1), _CONTROL, _NOON)

    row = next(iter(relationship.warmth(led, _CONTROL)))

    assert row.peer_id == 552  # noqa: PLR2004 -- the id written just above
    assert not hasattr(row, 'label')


# --------------------------------------- the two factors we MEASURE
# Irregularity (Whitchurch 2011; Ferster & Skinner 1957) and clumping
# (Bornstein/Berlyne) are not steered toward a target -- they are read off the
# gap statistics the store accumulates. These pin that the arithmetic reaches
# them at all: without a test here the two factors are functions nobody feeds,
# which is exactly the state they were in before.

_DAY = 86400.0
_BURST_GAP = 900.0
_TOUCHES = 40
_BATCH = 5
_FLAT_TOL = 0.01
_POISSON_MIN = 0.9


def _moments(gaps: list[float]) -> list[float]:
    """Turn a list of gaps into the absolute moments they produce."""
    out, now = [], _NOON
    for gap in gaps:
        now += gap
        out.append(now)
    return out


def _engage(led: relationship.Ledger, peer: int, gaps: list[float]) -> None:
    """Engage ``peer`` once per gap, the way decide_engage does it.

    One take per gap -- which counts the chance too, the way the engines do
    -- so the peer reaches ``warmth`` (which skips anyone who never offered)
    and the timing lands on a real ledger row.
    """
    for at in _moments(gaps):
        led.add_take(peer, _ids(1), _CONTROL, at)


def _poisson(seed: int) -> list[float]:
    """Memoryless gaps -- the reference this factor caps irregularity at."""
    rng = random.Random(seed)
    return [rng.expovariate(1 / _DAY) for _ in range(_TOUCHES)]


def test_metronome_timing_reads_as_perfectly_regular(tmp_path: Path) -> None:
    """Engaging on a fixed clock leaves irregularity at zero."""
    led = _ledger(tmp_path)
    _engage(led, 10, [_DAY] * _TOUCHES)
    assert relationship._irregularity(led.row(10)) < _FLAT_TOL


def test_memoryless_timing_reads_as_fully_irregular(tmp_path: Path) -> None:
    """A Poisson stream of gaps reaches the cap -- the Skinner reference."""
    led = _ledger(tmp_path)
    _engage(led, 11, _poisson(7))
    assert relationship._irregularity(led.row(11)) > _POISSON_MIN


def test_attention_all_at_once_reads_as_fully_clumped(tmp_path: Path) -> None:
    """Engagements inside one sitting are burst, whatever their count."""
    led = _ledger(tmp_path)
    _engage(led, 12, [60.0] * _TOUCHES)
    assert relationship._clumping(led.row(12)) == 1.0


def test_attention_spread_out_reads_as_unclumped(tmp_path: Path) -> None:
    """Gaps longer than burst_gap_sec are separate visits, not one."""
    led = _ledger(tmp_path)
    _engage(led, 13, [_BURST_GAP * 2] * _TOUCHES)
    assert relationship._clumping(led.row(13)) == 0.0


def test_a_batch_of_views_is_one_sitting_not_many_gaps(
    tmp_path: Path,
) -> None:
    """A story engine's batch adds ONE gap and n-1 burst, never n gaps.

    Counting the dwell between two stories as an interval would read as wild
    irregularity when it is in fact a single visit.
    """
    led = _ledger(tmp_path)
    led.add_take(14, _ids(1), _CONTROL, _NOON)  # first visit: no gap behind it
    led.add_take(14, _ids(_BATCH), _CONTROL, _NOON + _DAY)
    row = led.row(14)
    assert row.gap_n == 1  # one interval, not _BATCH of them
    assert row.gap_sum == _DAY
    assert row.burst == _BATCH - 1  # the batch's own members, nothing else


def test_warmth_index_carries_the_measured_factors(tmp_path: Path) -> None:
    """Two peers with identical p and r still differ in A~, by timing alone.

    This is what wiring v and c buys: an account that shows up irregularly
    and spread out is worth more than one that binges on a schedule, and the
    partial index could not tell the two apart.
    """
    led = _ledger(tmp_path)
    _engage(led, 12, [60.0] * _TOUCHES)
    _engage(led, 15, _poisson(3))
    for peer in (12, 15):
        led.add_recip(peer, _ids(_TOUCHES // 2), _NOON, _TZ)
    scored = {row.peer_id: row for row in relationship.warmth(led, _CONTROL)}
    assert scored[12].p == scored[15].p  # same exposure
    assert scored[12].r == scored[15].r  # same reciprocity
    assert scored[15].index > scored[12].index
