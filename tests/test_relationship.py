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
from minions.userbot.core.state import StateStore

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


def _ledger(tmp_path: Path) -> relationship.Ledger:
    """Return a ledger over a fresh store (the counters live in SQLite)."""
    store = StateStore(tmp_path / 'peers.db', tmp_path / 'cursors.json')
    return relationship.Ledger(store, 'reactions')


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
    peer = 'p'
    for _ in range(_STEPS):
        if rng.random() < led.take_prob(peer, ctrl):
            led.bump_take(peer)
        led.add_offer(peer)
    ratio = led.row(peer).taken / led.row(peer).offered
    assert abs(ratio - ctrl.take_target()) < _TOL


def test_recip_prob_converges_recip_taken_to_the_target(
    tmp_path: Path,
) -> None:
    """Repeated recip decisions drive recip/taken onto recip_target."""
    led = _ledger(tmp_path)
    ctrl = _steer_only()
    rng = random.Random(1)
    peer = 'p'
    for _ in range(_STEPS):
        led.bump_take(peer)  # every step is a taken exposure
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
    led.add_recip('p', 1, _NOON, _TZ)
    assert led.recip_left(ctrl, _NOON, _TZ) == _CAP - 1
    assert led.recip_left(ctrl, _NOON, _TZ) == _CAP - 1  # still, read-only


def test_warmth_orders_by_recency_and_evict_drops_a_peer(
    tmp_path: Path,
) -> None:
    """warmth() lists the most recent peer first; evict removes a peer."""
    led = _ledger(tmp_path)
    led.add_take('early', 2)
    led.add_recip('early', 1, _NOON, _TZ)
    led.add_offer('late', 3)  # interacted with more recently
    rows = relationship.warmth(led, _CONTROL)
    assert [row.label for row in rows] == ['late', 'early']  # newest first
    led.add_offer('early')  # touching 'early' again moves it to the front
    assert next(r.label for r in relationship.warmth(led, _CONTROL)) == 'early'
    led.evict('early')
    assert led.row('early').offered == 0


def test_remember_labels_warmth_and_ignores_the_bare_id(
    tmp_path: Path,
) -> None:
    """A cached @name shows in warmth; a label equal to the id is ignored."""
    led = _ledger(tmp_path)
    led.add_take('552', 1)
    led.remember('552', '@liriiu (552)')
    led.remember('552', '552')  # a failed resolve must not clobber the name
    assert next(r.label for r in relationship.warmth(led, _CONTROL)) == (
        '@liriiu (552)'
    )
