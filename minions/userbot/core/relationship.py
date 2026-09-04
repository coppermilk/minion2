# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Per-peer relationship control, shared by the story and like engines.

We steer how much each individual person warms to the account along the
Berlyne/Wundt curve (see ``engines/attachment.py``). Two engines pull the same
levers, so the logic lives here once:

- the STORY engine views a Wundt-peak fraction of a person's stories (exposure)
  and hearts a fraction of what it views (reciprocity);
- the LIKE engine likes a Wundt-peak fraction of a person's comments (exposure)
  and upgrades a fraction of those to a thread sticker (reciprocity).

Both reduce to one memory (``Ledger`` -- offers seen, exposures taken,
reciprocations) and one control law (``steer`` -- a clamped P-controller that
nudges a running fraction toward its target). The engines keep only what is
genuinely theirs: their own commit timing (the story engine decides at plan
time and commits when a view/react actually lands; the like engine commits at
decide time).

Two of the model's four factors are STEERED here -- exposure and reciprocity,
because a target exists to steer them to. The other two are only MEASURED, and
that is not a lesser status: ``_irregularity`` and ``_clumping`` read the gap
statistics the store accumulates and hand them to the same index. Deliberately
so -- steering timing toward a target would make the timing regular, which is
the one thing Whitchurch (2011) and Ferster & Skinner (1957) say costs you.
``core/attachment.py`` carries the citations for all four.

The counts live in ``core/state.StateStore``, one row per peer in the
engine's own database, so a bump writes one row rather than rewriting every
peer the account has ever met. The two engines used to serialise this same
shape into two files under different names -- ``commented/engaged/stickered``
against ``offered/viewed/reacted`` -- which is one concept wearing two
costumes.
The daily caps are NOT per peer, so they stay here as plain fields and ride
the engine's cursor block.

Telethon-free, so every decision is unit-testable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from dataclasses import replace
from typing import TYPE_CHECKING

from minions.userbot.core import attachment
from minions.userbot.core import codec
from minions.userbot.core import humanize
from minions.userbot.core import state

if TYPE_CHECKING:
    from collections.abc import Mapping


def _clip(value: float) -> float:
    """Clamp a probability into [0, 1]."""
    return min(1.0, max(0.0, value))


def steer(current: float, target: float, gain: float) -> float:
    """Return the corrective Bernoulli probability toward ``target``.

    The one control law both engines share: a peer already above the target is
    pushed down, one below is pulled up, so the running fraction converges on
    ``target``. A proportional controller clamped to a valid probability.
    """
    return _clip(target + gain * (target - current))


@dataclass(frozen=True)
class Warmth:
    """One peer's attachment readout for /status: exposure, recip, index.

    ``index`` is the FULL Berlyne index over all four factors. ``p`` and ``r``
    are the two we steer, so they are shown beside it; irregularity and
    clumping are measured, not steered, and fold into ``index`` alone.

    A peer is named by id and nothing else. Who they are lives in ``actors``
    and is looked up by whoever renders this -- a readout row used to carry
    a display label too, which meant the same person's name travelled beside
    every number about them.
    """

    peer_id: int
    p: float  # taken / offered (exposure)
    r: float  # recip / taken (reciprocity)
    index: float
    offered: int


DAY = 86400.0
"""Seconds in a day -- an arc leg is configured in days, not seconds."""


@dataclass(frozen=True)
class Leg:
    """One leg of the arc, and what it does to the two steered fractions.

    ``days`` is how long it lasts. ``exposure`` and ``recip`` are SHARES OF
    THE STEADY-STATE TARGET in [0, 1], not absolute fractions: 1.0 means the
    Wundt peak itself, 0.15 means a seventh of it. So the peak is the arc's
    CEILING by construction -- the warmest leg sits exactly on the optimum
    and no configuration can push a person past it into the aversion arm,
    where attention stops reading as interest and starts reading as
    surveillance. The arc withdraws attention; it never oversupplies it.

    The controller converges each person on whatever this resolves to,
    exactly as it always did. The steering law does not change at all --
    only what it is aimed at.
    """

    name: str
    days: float
    exposure: float
    recip: float


@dataclass(frozen=True)
class Arc:
    """The individual curve every person is walked around, on their own clock.

    Their clock, not ours: it starts the moment WE first act on THEM, so a
    person met today gets the same opening a person met last year got, and
    two people met a month apart are in different legs of it at the same
    instant. That is the whole point of "one curve per person" -- a global
    schedule would put the entire audience into a cold shoulder on the same
    Tuesday, which is a mood, not a relationship.

    ``legs`` runs in order and then repeats, and each repetition is a ROUND.
    The last leg is the one that keeps working: an unpredictable alternation
    between the two before it, redrawn once a day. Ferster & Skinner (1957)
    is already cited in ``core/attachment.py`` for exactly this -- a variable
    schedule is the one most resistant to extinction, and the fixed legs
    before it are what teach the two states it alternates BETWEEN.

    Holds no state of its own and stores nothing: a leg is a pure function of
    when we met a person, when it is now, and their id. So it survives a
    restart, is the same before and after a backup, and ``/who`` can say
    which leg somebody is in without a column to disagree with.
    """

    legs: tuple[Leg, ...]
    enabled: bool = False

    def leg(self, since: float, now: float, peer: int) -> Leg:
        """Return which leg of the arc a person is in right now.

        ``since`` is when we met them (0 = right now, so: the first leg).
        The swing leg redraws from the fixed legs once a local day, keyed on
        the person and the day, so it is stable within a day, different
        between two people on the same day, and needs nothing written down.
        """
        span = sum(leg.days for leg in self.legs) * DAY
        elapsed = max(0.0, now - since) if since > 0 else 0.0
        into = elapsed % span
        for leg in self.legs:
            if into < leg.days * DAY:
                return self._drawn(leg, now, peer)
            into -= leg.days * DAY
        return self.legs[-1]  # the modulo above cannot land here

    def rounds(self, since: float, now: float) -> int:
        """Return which time around the arc a person is on (1 = the first)."""
        span = sum(leg.days for leg in self.legs) * DAY
        elapsed = max(0.0, now - since) if since > 0 else 0.0
        return int(elapsed // span) + 1

    def _drawn(self, leg: Leg, now: float, peer: int) -> Leg:
        """Resolve the swing leg to one of the fixed legs for today.

        Any leg but the last is itself. The last one is the swing, and what
        it swings between is every leg before it -- so the alternation is
        made of states the person has already learned, which is what makes
        it read as a withdrawal of something rather than as noise.

        Weighted by each fixed leg's own ``days``, so the longer leg is also
        the more common face of the swing and no second set of knobs has to
        agree with the first. The drawn leg keeps its own name behind the
        swing's (``swing:cold``), because "which face is it today" is the
        one thing an operator reading /who actually wants to know.
        """
        if leg is not self.legs[-1] or len(self.legs) < _SWING_MIN:
            return leg
        fixed = self.legs[:-1]
        total = sum(one.days for one in fixed)
        cut = _spin(peer, int(now // DAY)) / _RANGE64 * total
        for one in fixed:
            if cut < one.days:
                return replace(one, name=f'{leg.name}:{one.name}')
            cut -= one.days
        return replace(fixed[-1], name=f'{leg.name}:{fixed[-1].name}')


NO_ARC = Arc(())
"""No arc at all: the steady Wundt peak forever, which is the old behaviour.

A shared frozen value rather than a default built per dataclass, so an engine
that never configures one carries the same object every other engine does and
"is there an arc" is a field read, not a construction.
"""

_SWING_MIN = 3
"""Legs needed before the last one can swing (two to alternate, plus it)."""

_MASK64 = (1 << 64) - 1
_RANGE64 = 1 << 64
_MIX_A = 0xFF51AFD7ED558CCD
_MIX_B = 0xC4CEB9FE1A85EC53
_ODD = 0x9E3779B97F4A7C15  # golden-ratio odd constant, so ids never collide


def _spin(peer: int, day: int) -> int:
    """Return a stable pseudo-random 64-bit draw from (person, day).

    Murmur3's finalizer, not ``hash()`` and not plain arithmetic. Both
    alternatives fail here in ways that would be hard to see:

    - ``hash()`` of anything string-shaped is salted per process, so the
      swing would re-roll on every restart -- our randomness, not a schedule
      the person on the other end can feel.
    - a linear mix (``peer * K + day``) alternates the low bit every day, so
      a two-leg swing becomes a strict odd/even metronome. Perfectly
      predictable, which is exactly what a variable schedule must not be --
      Ferster & Skinner's whole point is that the UNPREDICTABLE one is the
      one that does not extinguish.

    So the bits have to avalanche, and this is the standard way to do it.
    """
    x = ((peer & _MASK64) * _ODD ^ (day & _MASK64)) & _MASK64
    x = ((x ^ (x >> 33)) * _MIX_A) & _MASK64
    x = ((x ^ (x >> 33)) * _MIX_B) & _MASK64
    return x ^ (x >> 33)


def load_arc(cfg: Mapping[str, object]) -> Arc:
    """Build the arc from one engine's sub-config (``arc`` + ``arc_enabled``).

    Every leg names itself, so the config reads as the shape it describes and
    ``/who`` can print the name back without a second table mapping index to
    word. An empty or one-leg list is an arc that cannot alternate, so it is
    left disabled whatever the flag says -- a "swing" with nothing to swing
    between would silently be a constant, which is the failure mode this
    would be hardest to notice.
    """
    legs = tuple(
        Leg(
            name=codec.text(row.get('name')),
            days=codec.num(row.get('days')),
            exposure=codec.num(row.get('exposure')),
            recip=codec.num(row.get('recip')),
        )
        for row in (codec.table(item) for item in codec.rows(cfg.get('arc')))
    )
    on = bool(cfg.get('arc_enabled')) and len(legs) >= _LEGS_MIN
    return Arc(legs=legs, enabled=on)


_LEGS_MIN = 2
"""An arc needs at least two legs, or it is one constant with a name."""


@dataclass(frozen=True)
class Control:
    """The tunables of the relationship control (shared stories/likes).

    ``wundt``'s argmax is the STEADY-STATE exposure target (engage a person
    ~2/3 of the time -- the Wundt peak, not everything, since everything
    reads as stalking/desperation), and it is what an arc-less account
    steers to forever. ``recip_target`` is the same for the fraction of taken
    exposures answered with the stronger act (a heart, a sticker).

    ``arc`` scales both DOWN, per person, per leg -- see ``Arc``. It only
    ever scales down: a leg is a share of these two in [0, 1], so the peak
    stays the maximum any person is ever given and the arc is built out of
    how much attention is WITHHELD rather than out of overshoot. Which also
    keeps ``attachment.index`` honest -- the warmest leg sits on the argmax
    of the very curve the index scores against, so the readout peaks where
    the model says it should instead of dipping on its own aversion arm.

    ``take_cap`` / ``recip_cap`` are per-day ceilings on the two acts
    (0 = uncapped). ``burst_gap_sec`` is how close two engagements must be to
    count as one sitting rather than two -- it is what separates spread-out
    attention from massed attention for the clumping factor.
    """

    wundt: attachment.WundtParams
    take_gain: float = 1.0
    recip_target: float = 0.20
    recip_gain: float = 1.0
    take_cap: int = 0
    recip_cap: int = 0
    burst_gap_sec: float = 900.0
    arc: Arc = Arc(())

    def take_target(self, leg: Leg | None = None) -> float:
        """Return the exposure fraction to steer toward.

        The Wundt peak with no arc running, and that peak scaled by the
        leg's share when one is -- so the peak is the ceiling either way and
        the controller below does not know the difference.
        """
        peak = attachment.exposure_peak(self.wundt)
        return peak if leg is None else peak * _clip(leg.exposure)

    def stance(self, leg: Leg | None = None) -> str:
        """Return what we are DOING with somebody, as a rung of ``ACTS``.

        The readout's answer to "what is happening with this person", in one
        word, derived from what the controller is aimed at rather than from
        the leg's name -- so retuning a leg in the JSON moves the word with
        it instead of leaving a label that used to be true.

        Reading down the ladder: a leg that answers nothing is ``ignore``
        however much it watches; one that watches but never answers is
        ``seen``; one doing both is ``like``. The words are ``ACTS`` rungs
        because they are the same three acts the history is written in --
        the reader learns one vocabulary, not two.
        """
        passed, took, back = state.ACTS['stories']
        if self.take_target(leg) < self.wundt.c1:
            return passed
        return back if self.recip_goal(leg) > 0 else took

    def doing(self, leg: Leg | None, row: state.PeerRow) -> str:
        """Return what we are doing with ONE person, as a rung of ``ACTS``.

        The lesser of what this leg INTENDS and what has actually happened,
        because a readout claiming "liking" beside a 0% like column is not
        two views of one thing, it is a contradiction -- and it was the same
        word for everybody, since the intention is a property of the leg and
        every fresh account is in the same leg.

        Capping it by the record makes the word carry what the reader wanted
        from it. Somebody we have watched but never answered reads "seen"
        even inside a honeymoon, because that is what we have done to them;
        somebody in a cold shoulder reads "ignore" however warm their
        history, because that is what we are doing to them now.

        A person with no record at all is not being ignored, they are new,
        so the intention stands alone for them.
        """
        ladder = state.ACTS['stories']
        intend = ladder.index(self.stance(leg))
        if not row.offered:
            return ladder[intend]
        done = len([n for n in (row.taken, row.recip) if n])
        return ladder[min(intend, done)]

    def leg_name(self, leg: Leg | None = None) -> str:
        """Return the leg's name for a READER, not its name in the config.

        The two differ for exactly one leg. The fixed legs are phases the
        operator named and gave a length to, so ``honeymoon`` is what they
        are and what they should be called. The swing has no phase of its
        own -- it is redrawn daily -- so naming its face after whichever
        fixed leg it copied says the mechanism, when the only thing that
        matters is what the face DOES. ``swing:ignore`` beside a column
        reading "ignoring" is one vocabulary; ``swing:cold`` beside it was
        two words for one fact, and the reader had to learn the mapping.

        Derived here rather than baked in by ``Arc``, which knows nothing
        about the attachment curve the stance is measured against -- and
        because a name for a reader is a rendering, not a stored field.
        """
        if leg is None:
            return ''
        head, mark, _ = leg.name.partition(':')
        return f'{head}:{self.stance(leg)}' if mark else leg.name

    def recip_goal(self, leg: Leg | None = None) -> float:
        """Return the reciprocity fraction to steer toward (same rule)."""
        if leg is None:
            return self.recip_target
        return self.recip_target * _clip(leg.recip)


def _rolled(day: str, today: int, stamp: str) -> tuple[str, int]:
    """Return the daily counter reset to 0 when the local date changed."""
    return (stamp, 0) if day != stamp else (day, today)


@dataclass
class Ledger:
    """Per-peer relationship memory, kept in the engine's state store.

    ``offered`` is how many chances a peer gave us (their stories, their
    comments); ``taken`` how many we engaged (viewed, liked); ``recip`` how
    many we answered with the stronger act. ``taken`` is a subset of
    ``offered``, ``recip`` a subset of ``taken``. Those three live in the
    store, one row per peer, and are never trimmed, so a fraction stays true
    however much bounded history an engine throws away.

    The date-keyed counters below back the ban-surface caps. They are one
    number each, not per peer, so they stay in memory and ride the engine's
    cursor block.
    """

    store: state.StateStore
    take_day: str = ''
    take_today: int = 0
    recip_day: str = ''
    recip_today: int = 0

    def row(self, peer: int) -> state.PeerRow:
        """Return a peer's standing (all zeroes if we have not met them)."""
        return self.store.peer(peer)

    def leg(self, peer: int, control: Control, now: float) -> Leg | None:
        """Return which leg of their arc a person is in, or None if it is off.

        One query for when we met them, then pure arithmetic -- so this is
        cheap enough to ask per decision and there is no leg column anywhere
        to drift out of step with the clock that defines it.
        """
        if not control.arc.enabled:
            return None
        return control.arc.leg(self.store.met(peer), now, peer)

    def take_prob(
        self, peer: int, control: Control, now: float = 0.0
    ) -> float:
        """Exposure probability for one more of ``peer``'s offers.

        Uses the fraction recorded BEFORE this offer, so the caller computes
        it first and records the offer after. ``now`` places them on their
        own arc; left out, the steady-state Wundt peak is the target, which
        is what an account with no arc configured does forever.
        """
        row = self.row(peer)
        p_star = control.take_target(self.leg(peer, control, now))
        p_cur = row.taken / row.offered if row.offered else p_star
        # Never above what this leg aims at. The controller corrects the
        # LIFETIME fraction, so a honeymoon following a cold shoulder found
        # itself dragging 43% up to the peak and rolled 92% to do it --
        # opening nearly every story, which is the aversion arm the peak
        # exists to stay off. The target is the ceiling by construction
        # everywhere else in this file; clamping here makes that true of the
        # dice too, and not only of the number they are aimed at.
        return min(p_star, steer(p_cur, p_star, control.take_gain))

    def recip_prob(  # noqa: PLR0913 -- peer + control + now + the one flag
        self,
        peer: int,
        control: Control,
        now: float = 0.0,
        *,
        taken_now: bool,
    ) -> float:
        """Reciprocity probability among ``peer``'s taken exposures.

        ``taken_now`` is True when ``taken`` already counts the exposure being
        decided (the like engine commits the take first), so the running
        fraction excludes it.
        """
        row = self.row(peer)
        taken = max(0, row.taken - 1) if taken_now else row.taken
        r_cur = row.recip / (taken or 1)
        goal = control.recip_goal(self.leg(peer, control, now))
        return steer(r_cur, goal, control.recip_gain)

    def add_offer(
        self,
        peer: int,
        subjects: tuple[int, ...] = (),
        now: float | None = None,
    ) -> None:
        """Record the chances ``peer`` gave us that we did NOT take.

        The other half of ``add_take``: both are called once per chance, at
        the moment the answer is known, so every chance leaves exactly one
        row saying which way it went (``ignore`` here, the middle rung
        there).

        ``subjects`` names them -- the story ids, the comment's message id --
        and its length is the count. A caller with nothing to name passes
        one empty subject rather than a bare number, so the log always has a
        row per offer even when it cannot say which.

        ``now`` is the engine's moment; it moves the peer to the front of the
        recency order without touching ``take_at``, which only our own
        engagements advance.
        """
        self.store.bump(peer, {'offered': len(subjects)}, now, subjects)

    def add_take(  # noqa: PLR0913 -- peer + what + (control, now) read best flat
        self,
        peer: int,
        subjects: tuple[int, ...],
        control: Control,
        now: float,
    ) -> None:
        """Record the exposures taken (which also counts them offered)."""
        n = len(subjects)
        counts: dict[str, float] = {'offered': n, 'taken': n}
        counts |= _gap_stats(self._gap(peer, now), n, control.burst_gap_sec)
        self.store.bump(peer, counts, now, subjects)

    def _gap(self, peer: int, now: float) -> float:
        """Seconds since we last engaged ``peer``; 0 when this is the first.

        Measured from ``take_at``, not ``last_at``: the offer that precedes an
        engagement lands at the same instant, so ``last_at`` would make every
        gap zero.
        """
        last = self.row(peer).take_at
        return now - last if last > 0.0 else 0.0

    def bump_recip(
        self, peer: int, subject: int = 0, now: float | None = None
    ) -> None:
        """Count one reciprocation whose daily slot is already spent."""
        self.store.bump(peer, {'recip': 1}, now, (subject,))

    def add_recip(  # noqa: PLR0913 -- peer + what + (now, tz) read best flat
        self, peer: int, subjects: tuple[int, ...], now: float, tz: float
    ) -> None:
        """Record the reciprocations to ``peer`` and the daily counter."""
        stamp = humanize.local(now, tz).date().isoformat()
        self.recip_day, self.recip_today = _rolled(
            self.recip_day, self.recip_today, stamp
        )
        self.recip_today += len(subjects)
        self.store.bump(peer, {'recip': len(subjects)}, now, subjects)

    def spend_take(self, control: Control, now: float, tz: float) -> bool:
        """Consume one take slot from today's budget; False when capped."""
        stamp = humanize.local(now, tz).date().isoformat()
        self.take_day, self.take_today = _rolled(
            self.take_day, self.take_today, stamp
        )
        if control.take_cap > 0 and self.take_today >= control.take_cap:
            return False
        self.take_today += 1
        return True

    def spend_recip(self, control: Control, now: float, tz: float) -> bool:
        """Consume one recip slot from today's budget; False when capped."""
        stamp = humanize.local(now, tz).date().isoformat()
        self.recip_day, self.recip_today = _rolled(
            self.recip_day, self.recip_today, stamp
        )
        if control.recip_cap > 0 and self.recip_today >= control.recip_cap:
            return False
        self.recip_today += 1
        return True

    def recip_left(self, control: Control, now: float, tz: float) -> int:
        """Return how many reciprocations are still allowed today.

        Read-only (does not consume): the story engine plans a batch against
        the remaining budget, then commits with ``add_recip`` when reactions
        actually go out.
        """
        if control.recip_cap <= 0:
            return 0
        stamp = humanize.local(now, tz).date().isoformat()
        used = self.recip_today if self.recip_day == stamp else 0
        return max(0, control.recip_cap - used)

    def takes_today(self, now: float, tz: float) -> int:
        """Exposures taken on the local date of ``now`` (else 0)."""
        stamp = humanize.local(now, tz).date().isoformat()
        return self.take_today if self.take_day == stamp else 0

    def recips_today(self, now: float, tz: float) -> int:
        """Reciprocations made on the local date of ``now`` (else 0)."""
        stamp = humanize.local(now, tz).date().isoformat()
        return self.recip_today if self.recip_day == stamp else 0

    def evict(self, peer: int) -> None:
        """Drop a peer's counters with this engine (rolled off the set)."""
        self.store.forget(peer)

    def counters(self) -> dict[str, object]:
        """Return the daily counters for the engine's cursor block."""
        return {
            'take_day': self.take_day,
            'take_today': self.take_today,
            'recip_day': self.recip_day,
            'recip_today': self.recip_today,
        }

    def restore(self, cursor: Mapping[str, object]) -> None:
        """Reload the daily counters from the engine's cursor block."""
        self.take_day = codec.text(cursor.get('take_day'))
        self.take_today = codec.whole(cursor.get('take_today'))
        self.recip_day = codec.text(cursor.get('recip_day'))
        self.recip_today = codec.whole(cursor.get('recip_today'))


_MIN_GAPS = 2
"""Two intervals is the fewest that can have a spread between them."""


def _gap_stats(gap: float, n: int, burst_gap: float) -> dict[str, float]:
    """Additive timing deltas for ``n`` engagements arriving as one sitting.

    A batch contributes ONE gap -- the wait since the previous sitting -- plus
    ``n - 1`` engagements that are inside the current one by definition. Their
    own spacing is the couple of seconds between two stories, and counting
    those as gaps would read as wild irregularity instead of as a single
    visit. The first engagement of all has no interval behind it to measure.
    """
    if gap <= 0.0:
        return {'burst': n - 1}
    return {
        'gap_n': 1,
        'gap_sum': gap,
        'gap_sq': gap * gap,
        'burst': int(gap < burst_gap) + n - 1,
    }


def _irregularity(row: state.PeerRow) -> float:
    """How unpredictable our attention to this peer is, in [0, 1].

    The coefficient of variation of the gaps, capped at 1. CV = 0 is a
    metronome; CV = 1 is a memoryless (Poisson) process -- the variable
    schedule Ferster & Skinner found hardest to extinguish, and the point past
    which more raggedness carries no further signal. Sums, not a history: the
    dispersion comes out of gap_sum/gap_sq, which cost two numbers per peer.
    """
    if row.gap_n < _MIN_GAPS:
        return 0.0
    mean = row.gap_sum / row.gap_n
    if mean <= 0.0:
        return 0.0
    var = max(0.0, row.gap_sq / row.gap_n - mean * mean)
    return min(1.0, math.sqrt(var) / mean)


def _clumping(row: state.PeerRow) -> float:
    """Return the share of our attention that arrived inside one sitting.

    Every engagement but the first could have joined one, so that is the
    denominator: 0 is attention spread out, 1 is all of it in single bursts.
    """
    if row.taken <= 1:
        return 0.0
    return min(1.0, row.burst / (row.taken - 1))


def warmth(ledger: Ledger, control: Control) -> list[Warmth]:
    """Per-peer attachment readout, MOST RECENT peer first.

    Ordered by recency (the last-interacted-with peer first), not by score,
    so /status shows the people we just engaged rather than an all-time hall
    of fame -- and that order is now a column, where it used to depend on
    dict insertion order. ``index`` is the full Berlyne index over all four
    factors; a peer needs at least one recorded offer to appear.
    """
    rows: list[Warmth] = []
    for row in ledger.store.peers():
        if row.offered <= 0:
            continue
        p = row.taken / row.offered
        r = row.recip / row.taken if row.taken else 0.0
        factors = attachment.Factors(
            p=p, v=_irregularity(row), r=r, c=_clumping(row)
        )
        idx = attachment.attachment_index(factors, control.wundt)
        rows.append(Warmth(row.peer_id, p, r, idx, row.offered))
    return rows
