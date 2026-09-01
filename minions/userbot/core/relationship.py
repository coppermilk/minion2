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

The counts live in ``core/state.StateStore``, one row per (engine, peer),
so a bump writes one row rather than rewriting every peer the account has
ever met. The two engines used to serialise this same shape into two files
under different names -- ``commented/engaged/stickered`` against
``offered/viewed/reacted`` -- which is one concept wearing two costumes.
The daily caps are NOT per peer, so they stay here as plain fields and ride
the engine's cursor block.

Telethon-free, so every decision is unit-testable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
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
    """

    label: str
    peer_id: str  # the raw id, so a readout can strip it back off the label
    p: float  # taken / offered (exposure)
    r: float  # recip / taken (reciprocity)
    index: float
    offered: int


@dataclass(frozen=True)
class Control:
    """The tunables of the relationship control (shared stories/likes).

    ``wundt``'s argmax IS the exposure target (engage a person ~2/3 of the
    time -- the Wundt peak, not everything, since everything reads as
    stalking/desperation). ``recip_target`` is the fraction of taken exposures
    answered with the stronger act (a heart, a sticker). ``take_cap`` /
    ``recip_cap`` are per-day ceilings on the two acts (0 = uncapped).
    ``burst_gap_sec`` is how close two engagements must be to count as one
    sitting rather than two -- it is what separates spread-out attention from
    massed attention for the clumping factor.
    """

    wundt: attachment.WundtParams
    take_gain: float = 1.0
    recip_target: float = 0.20
    recip_gain: float = 1.0
    take_cap: int = 0
    recip_cap: int = 0
    burst_gap_sec: float = 900.0

    def take_target(self) -> float:
        """Return the exposure fraction to steer toward (the Wundt peak)."""
        return attachment.exposure_peak(self.wundt)


def _rolled(day: str, today: int, stamp: str) -> tuple[str, int]:
    """Return the daily counter reset to 0 when the local date changed."""
    return (stamp, 0) if day != stamp else (day, today)


@dataclass
class Ledger:
    """Per-peer relationship memory, kept in the shared state store.

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
    engine: str
    take_day: str = ''
    take_today: int = 0
    recip_day: str = ''
    recip_today: int = 0

    def row(self, peer: str) -> state.PeerRow:
        """Return a peer's standing (all zeroes if we have not met them)."""
        return self.store.peer(self.engine, peer)

    def take_prob(self, peer: str, control: Control) -> float:
        """Exposure probability for one more of ``peer``'s offers.

        Uses the fraction recorded BEFORE this offer, so the caller computes
        it first and records the offer after.
        """
        row = self.row(peer)
        p_star = control.take_target()
        p_cur = row.taken / row.offered if row.offered else p_star
        return steer(p_cur, p_star, control.take_gain)

    def recip_prob(
        self, peer: str, control: Control, *, taken_now: bool
    ) -> float:
        """Reciprocity probability among ``peer``'s taken exposures.

        ``taken_now`` is True when ``taken`` already counts the exposure being
        decided (the like engine commits the take first), so the running
        fraction excludes it.
        """
        row = self.row(peer)
        taken = max(0, row.taken - 1) if taken_now else row.taken
        r_cur = row.recip / (taken or 1)
        return steer(r_cur, control.recip_target, control.recip_gain)

    def add_offer(
        self, peer: str, n: int = 1, now: float | None = None
    ) -> None:
        """Record ``n`` more chances from ``peer`` (offers, not taken).

        ``now`` is the engine's moment; it moves the peer to the front of the
        recency order without touching ``take_at``, which only our own
        engagements advance.
        """
        self.store.bump(self.engine, peer, {'offered': n}, now)

    def add_take(  # noqa: PLR0913 -- peer + count + (control, now) read best flat
        self, peer: str, n: int, control: Control, now: float
    ) -> None:
        """Record ``n`` exposures actually taken (also counts them offered)."""
        counts: dict[str, float] = {'offered': n, 'taken': n}
        counts |= _gap_stats(self._gap(peer, now), n, control.burst_gap_sec)
        self.store.bump(self.engine, peer, counts, now, take_at=now)

    def bump_take(self, peer: str, control: Control, now: float) -> None:
        """Count one already-offered exposure as taken (decide-time commit)."""
        counts: dict[str, float] = {'taken': 1}
        counts |= _gap_stats(self._gap(peer, now), 1, control.burst_gap_sec)
        self.store.bump(self.engine, peer, counts, now, take_at=now)

    def _gap(self, peer: str, now: float) -> float:
        """Seconds since we last engaged ``peer``; 0 when this is the first.

        Measured from ``take_at``, not ``last_at``: the offer that precedes an
        engagement lands at the same instant, so ``last_at`` would make every
        gap zero.
        """
        last = self.row(peer).take_at
        return now - last if last > 0.0 else 0.0

    def bump_recip(self, peer: str, now: float | None = None) -> None:
        """Count one reciprocation whose daily slot is already spent."""
        self.store.bump(self.engine, peer, {'recip': 1}, now)

    def add_recip(  # noqa: PLR0913 -- peer + count + (now, tz) read best flat
        self, peer: str, n: int, now: float, tz: float
    ) -> None:
        """Record ``n`` reciprocations to ``peer`` and the daily counter."""
        stamp = humanize.local(now, tz).date().isoformat()
        self.recip_day, self.recip_today = _rolled(
            self.recip_day, self.recip_today, stamp
        )
        self.recip_today += n
        self.store.bump(self.engine, peer, {'recip': n}, now)

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

    def remember(self, peer: str, label: str) -> None:
        """Cache ``peer``'s display label (@name / title) for /status."""
        self.store.remember(self.engine, peer, label)

    def evict(self, peer: str) -> None:
        """Drop a peer's counters and cached name (rolled off the set)."""
        self.store.forget(self.engine, peer)

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
    for row in ledger.store.peers(ledger.engine):
        if row.offered <= 0:
            continue
        p = row.taken / row.offered
        r = row.recip / row.taken if row.taken else 0.0
        factors = attachment.Factors(
            p=p, v=_irregularity(row), r=r, c=_clumping(row)
        )
        idx = attachment.attachment_index(factors, control.wundt)
        rows.append(
            Warmth(
                row.label or row.peer_id, row.peer_id, p, r, idx, row.offered
            )
        )
    return rows
