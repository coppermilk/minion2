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

from dataclasses import dataclass
from typing import TYPE_CHECKING

from minions.userbot.core import attachment
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

    ``index`` is the partial Berlyne index exposure(p) * recip(r) -- the two
    factors steered per peer (variety/mass_pen are not tracked per peer, so
    they are left out rather than assumed).
    """

    label: str
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
    """

    wundt: attachment.WundtParams
    take_gain: float = 1.0
    recip_target: float = 0.20
    recip_gain: float = 1.0
    take_cap: int = 0
    recip_cap: int = 0

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

    def add_offer(self, peer: str, n: int = 1) -> None:
        """Record ``n`` more chances from ``peer`` (offers, not taken)."""
        self.store.bump(self.engine, peer, {'offered': n})

    def add_take(self, peer: str, n: int) -> None:
        """Record ``n`` exposures actually taken (also counts them offered)."""
        self.store.bump(self.engine, peer, {'offered': n, 'taken': n})

    def bump_take(self, peer: str) -> None:
        """Count one already-offered exposure as taken (decide-time commit)."""
        self.store.bump(self.engine, peer, {'taken': 1})

    def bump_recip(self, peer: str) -> None:
        """Count one reciprocation whose daily slot is already spent."""
        self.store.bump(self.engine, peer, {'recip': 1})

    def add_recip(  # noqa: PLR0913,PLR0917 -- peer + count + (now, tz) read best flat
        self, peer: str, n: int, now: float, tz: float
    ) -> None:
        """Record ``n`` reciprocations to ``peer`` and the daily counter."""
        stamp = humanize.local(now, tz).date().isoformat()
        self.recip_day, self.recip_today = _rolled(
            self.recip_day, self.recip_today, stamp
        )
        self.recip_today += n
        self.store.bump(self.engine, peer, {'recip': n})

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
        self.take_day = str(cursor.get('take_day', ''))
        self.take_today = int(cursor.get('take_today', 0) or 0)
        self.recip_day = str(cursor.get('recip_day', ''))
        self.recip_today = int(cursor.get('recip_today', 0) or 0)


def warmth(ledger: Ledger, control: Control) -> list[Warmth]:
    """Per-peer attachment readout, MOST RECENT peer first.

    Ordered by recency (the last-interacted-with peer first), not by score,
    so /status shows the people we just engaged rather than an all-time hall
    of fame -- and that order is now a column, where it used to depend on
    dict insertion order. ``index`` is the partial Berlyne index
    exposure(p) * recip(r); a peer needs at least one recorded offer to
    appear.
    """
    rows: list[Warmth] = []
    for row in ledger.store.peers(ledger.engine):
        if row.offered <= 0:
            continue
        p = row.taken / row.offered
        r = row.recip / row.taken if row.taken else 0.0
        idx = attachment.exposure(p, control.wundt) * attachment.recip(r)
        rows.append(Warmth(row.label or row.peer_id, p, r, idx, row.offered))
    return rows
