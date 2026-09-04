# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The shared per-peer relationship control (engines/relationship.py).

The story and like engines both drive this: one memory (``Ledger``) and one
control law (``steer``). These pin the shared kernel independently of either
engine's plan/commit wiring.
"""

from __future__ import annotations

import random
from dataclasses import replace
from itertools import pairwise
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
_TWO_ROUNDS = 2
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


# ------------------------------------------- the arc: one curve per person
# Honeymoon, cold shoulder, then an unpredictable swing between the two --
# per person, on the clock that starts when we first act on THEM.

_MET = 1_760_000_000.0
_ARC = relationship.Arc(
    legs=(
        relationship.Leg('honeymoon', days=14, exposure=1.0, recip=1.0),
        relationship.Leg('cold', days=10, exposure=0.12, recip=0.0),
        relationship.Leg('swing', days=21, exposure=0.0, recip=0.0),
    ),
    enabled=True,
)
_HONEYMOON_END = 13
_COLD_DAY = 18
_SWING_DAY = 30
_ROUND_TWO = 50
_SWING_SPAN = range(24, 45)


def _at(day: float) -> float:
    """Return the moment ``day`` days after we met somebody."""
    return _MET + day * relationship.DAY


def _arc_control(arc: relationship.Arc) -> relationship.Control:
    """Return a control with no caps, running ``arc``."""
    return relationship.Control(
        wundt=attachment.WundtParams(), recip_target=_TARGET, arc=arc
    )


def test_the_wundt_peak_is_the_ceiling_of_the_whole_arc() -> None:
    """No leg, however configured, aims a person past the peak.

    The arc is built out of attention WITHHELD, not attention oversupplied:
    a leg is a share in [0, 1] of the steady target, so the warmest one sits
    exactly on the argmax of the curve the index scores against. Past it is
    the aversion arm, where more attention reads as surveillance rather than
    as interest -- and a config typo must not be able to go there.
    """
    greedy = relationship.Arc(
        legs=(
            relationship.Leg('greedy', days=1, exposure=5.0, recip=5.0),
            relationship.Leg('cold', days=1, exposure=0.1, recip=0.0),
        ),
        enabled=True,
    )
    ctrl = _arc_control(greedy)
    peak = ctrl.take_target()

    aimed = [ctrl.take_target(leg) for leg in greedy.legs]
    backs = [ctrl.recip_goal(leg) for leg in greedy.legs]

    assert max(aimed) == peak
    assert max(backs) == _TARGET


def test_the_clock_starts_when_we_met_them_not_when_the_bot_did() -> None:
    """Two people met a month apart are in different legs the same evening.

    The whole point of a curve PER PERSON. One schedule for everybody would
    put the entire audience into a cold shoulder on the same Tuesday, which
    is a mood, not a relationship.
    """
    evening = _at(_COLD_DAY)
    old_friend = _ARC.leg(_MET, evening, 111)
    just_met = _ARC.leg(evening, evening, 222)

    assert old_friend.name == 'cold'
    assert just_met.name == 'honeymoon'


def test_a_person_with_no_history_at_all_starts_at_the_beginning() -> None:
    """Zero is not a missing value -- it is somebody we are meeting now."""
    assert _ARC.leg(0.0, _at(_SWING_DAY), 111).name == 'honeymoon'


def test_the_legs_run_in_order_and_then_go_round_again() -> None:
    """One trip through the legs is a round, and the rounds keep counting."""
    names = [_ARC.leg(_MET, _at(d), 111).name for d in (0, _COLD_DAY)]

    assert names == ['honeymoon', 'cold']
    assert _ARC.leg(_MET, _at(_HONEYMOON_END), 111).name == 'honeymoon'
    assert _ARC.rounds(_MET, _at(0)) == 1
    assert _ARC.rounds(_MET, _at(_ROUND_TWO)) == _TWO_ROUNDS
    assert _ARC.leg(_MET, _at(_ROUND_TWO), 111).name == 'honeymoon'


def test_the_swing_is_unpredictable_and_not_a_metronome() -> None:
    """A strict daily alternation is a schedule anyone can learn.

    Ferster & Skinner (1957) -- cited in ``core/attachment.py`` for exactly
    this -- is about the VARIABLE schedule being the one that does not
    extinguish. A cheap mix of person and day alternates the low bit every
    day and gives a perfect odd/even metronome, so the bits have to
    avalanche, and this is what says they do: the same face has to repeat on
    consecutive days at least once.
    """
    faces = [_ARC.leg(_MET, _at(d), 111).name for d in _SWING_SPAN]

    assert all(name.startswith('swing:') for name in faces)
    assert {name.split(':')[1] for name in faces} == {'honeymoon', 'cold'}
    assert any(a == b for a, b in pairwise(faces))


def test_the_swing_survives_a_restart_and_differs_between_people() -> None:
    """Stable per (person, day): our randomness would not be a schedule.

    Asked twice it answers the same, so a restart mid-swing does not re-roll
    the day -- and two people never share a swing, or the "unpredictable"
    leg would be one coin flipped for the whole audience.
    """
    days = list(_SWING_SPAN)
    once = [_ARC.leg(_MET, _at(d), 111).name for d in days]
    again = [_ARC.leg(_MET, _at(d), 111).name for d in days]
    somebody_else = [_ARC.leg(_MET, _at(d), 222).name for d in days]

    assert once == again
    assert once != somebody_else


def test_an_arc_that_is_off_aims_at_the_wundt_peak_forever() -> None:
    """No arc configured is the behaviour every account had before one."""
    ctrl = _arc_control(relationship.NO_ARC)
    assert ctrl.take_target(None) == attachment.exposure_peak(ctrl.wundt)
    assert ctrl.recip_goal(None) == _TARGET


def test_an_arc_with_nothing_to_alternate_stays_off(tmp_path: Path) -> None:
    """One leg is a constant wearing a curve's name, so it never turns on.

    The flag says yes and the list cannot deliver: silently steering
    everybody at one hard-coded fraction under the name "arc" is the failure
    this would be hardest to notice from the outside.
    """
    one = relationship.load_arc(
        {'arc_enabled': True, 'arc': [{'name': 'only', 'days': 3}]}
    )
    none = relationship.load_arc({'arc_enabled': True, 'arc': []})

    assert one.enabled is False
    assert none.enabled is False


def test_the_ledger_puts_each_peer_on_their_own_leg(tmp_path: Path) -> None:
    """End to end: the steering target follows the person's own history.

    ``met`` reads the contact log, so the arc is anchored on a fact already
    written rather than on a column that could disagree with it.
    """
    led = _ledger(tmp_path)
    ctrl = _arc_control(_ARC)
    led.add_take(111, _ids(1), _CONTROL, _MET)  # met a fortnight ago

    warm = led.take_prob(111, ctrl, _at(1))
    cold = led.take_prob(111, ctrl, _at(_COLD_DAY))

    assert cold < warm
    assert led.leg(111, ctrl, _at(_COLD_DAY)).name == 'cold'
    assert led.leg(111, _arc_control(relationship.NO_ARC), _at(1)) is None


def test_a_swing_face_is_named_for_what_it_does(tmp_path: Path) -> None:
    """``swing:ignore``, not ``swing:cold`` -- the reader gets one vocabulary.

    The fixed legs keep the names the operator gave them: those are phases
    with a length, and ``honeymoon`` is what one IS. The swing has no phase
    of its own, so naming its face after whichever leg it copied describes
    the mechanism, when the only thing that matters is what the face does --
    and it sat beside a column already reading "ignoring", which made two
    words for one fact and a mapping the reader had to learn.
    """
    ctrl = _arc_control(_ARC)
    faces = {
        ctrl.leg_name(_ARC.leg(_MET, _at(day), 111)) for day in _SWING_SPAN
    }

    assert faces == {'swing:ignore', 'swing:like'}
    assert ctrl.leg_name(_ARC.legs[0]) == 'honeymoon'  # a phase keeps its name
    assert ctrl.leg_name(None) == ''


def test_the_face_name_follows_the_config_not_the_leg_it_copied(
    tmp_path: Path,
) -> None:
    """Retune the leg the swing draws from and its face is renamed with it.

    The name is derived from what the controller is aimed at, so it cannot
    become a label that used to be true -- which is the whole reason it is
    not stored.
    """
    quiet = relationship.Arc(
        legs=(
            replace(_ARC.legs[0], recip=0.0),  # warm, but answers nothing
            _ARC.legs[1],
            _ARC.legs[2],
        ),
        enabled=True,
    )
    ctrl = _arc_control(quiet)

    faces = {
        ctrl.leg_name(quiet.leg(_MET, _at(day), 111)) for day in _SWING_SPAN
    }

    assert faces == {'swing:ignore', 'swing:seen'}


def test_the_dice_never_roll_above_what_the_leg_aims_at(
    tmp_path: Path,
) -> None:
    """The peak is the ceiling for the ROLL, not only for the target.

    The controller corrects the LIFETIME fraction, so a honeymoon following
    a cold shoulder found itself dragging 43% up to the peak and rolled 92%
    to do it -- opening nearly every story, which is the aversion arm the
    peak exists to stay off. Everywhere else in this module the target is
    the ceiling by construction; this asserts the dice obey it too.
    """
    led = _ledger(tmp_path)
    ctrl = _arc_control(_ARC)
    peer = 111
    led.add_take(peer, _ids(1), _CONTROL, _MET)
    # A long cold shoulder: many chances, almost none taken.
    for _ in range(_STEPS // 30):
        led.add_offer(peer, _ids(1), _at(_COLD_DAY))

    behind = led.row(peer)
    warm = led.take_prob(peer, ctrl, _at(1))  # back in the honeymoon

    assert behind.taken / behind.offered < _TOL * 2  # genuinely far behind
    assert warm == ctrl.take_target(_ARC.legs[0])  # and still only the peak


def test_a_peer_below_target_is_still_pulled_up_to_it(
    tmp_path: Path,
) -> None:
    """Clamping the roll must not stop it converging, only overshooting.

    Rolling AT the target is what makes a running fraction converge ON the
    target; the overshoot only ever bought speed, and bought it by leaving
    the curve the whole model is built on.
    """
    led = _ledger(tmp_path)
    ctrl = _steer_only()
    rng = random.Random(5)
    peer = 222
    for _ in range(_STEPS):
        if rng.random() < led.take_prob(peer, ctrl):
            led.add_take(peer, _ids(1), _CONTROL, _NOON)
        else:
            led.add_offer(peer, _ids(1))

    row = led.row(peer)
    assert abs(row.taken / row.offered - ctrl.take_target()) < _TOL


def test_the_word_never_claims_more_than_we_have_done(
    tmp_path: Path,
) -> None:
    """A claim of liking beside a 0% column is a contradiction.

    The intention belongs to the LEG, so on a fresh account every person is
    in the same leg and the word was identical for all of them -- carrying
    no information at the exact moment a reader most wants some. Capped by
    the record it says what has actually happened with each.
    """
    led = _ledger(tmp_path)
    ctrl = _arc_control(_ARC)
    warm = _ARC.legs[0]  # a leg that intends to like
    watched, liked, passed = 1, 2, 3

    led.add_take(watched, _ids(3), _CONTROL, _MET)  # seen, never answered
    led.add_take(liked, _ids(3), _CONTROL, _MET)
    led.add_recip(liked, _ids(1), _MET, _TZ)
    led.add_offer(passed, _ids(3), _MET)  # offered, never taken

    assert ctrl.doing(warm, led.row(watched)) == 'seen'
    assert ctrl.doing(warm, led.row(liked)) == 'like'
    assert ctrl.doing(warm, led.row(passed)) == 'ignore'


def test_a_cold_leg_says_ignore_however_warm_the_history(
    tmp_path: Path,
) -> None:
    """The word is what we are doing NOW, so the leg can only pull it down."""
    led = _ledger(tmp_path)
    ctrl = _arc_control(_ARC)
    peer = 9
    led.add_take(peer, _ids(3), _CONTROL, _MET)
    led.add_recip(peer, _ids(3), _MET, _TZ)  # a long, warm record

    assert ctrl.doing(_ARC.legs[0], led.row(peer)) == 'like'
    assert ctrl.doing(_ARC.legs[1], led.row(peer)) == 'ignore'


def test_somebody_we_have_never_acted_on_is_new_not_ignored(
    tmp_path: Path,
) -> None:
    """No record is not a record of neglect -- the intention stands alone."""
    ctrl = _arc_control(_ARC)
    assert ctrl.doing(_ARC.legs[0], _ledger(tmp_path).row(404)) == 'like'
