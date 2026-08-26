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
genuinely theirs: their I/O, their dedup (seen ids / catted keys), and their
own commit timing (the story engine decides at plan time and commits when a
view/react actually lands; the like engine commits at decide time).

Stdlib-only and Telethon-free, so every decision is unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

from minions.aggregator.core import humanize_time
from minions.aggregator.engines import attachment


def int_map(raw: object) -> dict[str, int]:
    """Coerce a persisted ``{peer: count}`` map to ``dict[str, int]``."""
    if not isinstance(raw, dict):
        return {}
    return {str(k): int(v) for k, v in raw.items()}


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
    """Per-peer relationship memory shared by the story and like engines.

    ``offered`` is how many chances a peer gave us (their stories, their
    comments); ``taken`` how many we engaged (viewed, liked); ``recip`` how
    many we answered with the stronger act (a heart, a sticker). ``taken`` is a
    subset of ``offered``, ``recip`` a subset of ``taken``. The per-day
    counters back the ban-surface caps. The counters are unbounded, so a
    fraction stays true even when a bounded ``seen`` list is trimmed.
    """

    offered: dict[str, int] = field(default_factory=dict)
    taken: dict[str, int] = field(default_factory=dict)
    recip: dict[str, int] = field(default_factory=dict)
    take_day: str = ''
    take_today: int = 0
    recip_day: str = ''
    recip_today: int = 0
    # Peer id -> a display label (@name / title), so /status shows names, not
    # raw ids -- the same readout the story and like engines share. Populated
    # as each engine learns a peer's name (a story view, a resolved commenter).
    names: dict[str, str] = field(default_factory=dict)

    def take_prob(self, peer: str, control: Control) -> float:
        """Exposure probability for one more of ``peer``'s offers.

        Uses the fraction recorded BEFORE this offer, so the caller computes it
        first and records the offer after.
        """
        offered = self.offered.get(peer, 0)
        p_star = control.take_target()
        p_cur = self.taken.get(peer, 0) / offered if offered else p_star
        return steer(p_cur, p_star, control.take_gain)

    def recip_prob(
        self, peer: str, control: Control, *, taken_now: bool
    ) -> float:
        """Reciprocity probability among ``peer``'s taken exposures.

        ``taken_now`` is True when ``taken`` already counts the exposure being
        decided (the like engine commits the take first), so the running
        fraction excludes it.
        """
        taken = self.taken.get(peer, 0)
        if taken_now:
            taken = max(0, taken - 1)
        r_cur = self.recip.get(peer, 0) / (taken or 1)
        return steer(r_cur, control.recip_target, control.recip_gain)

    def add_offer(self, peer: str, n: int = 1) -> None:
        """Record ``n`` more chances from ``peer`` (offers, not taken).

        Re-inserts the peer at the end of ``offered`` so its dict order tracks
        recency (most recently interacted-with last), which is what the
        /status readout shows -- the latest people, not the top-scored.
        """
        self.offered[peer] = self.offered.pop(peer, 0) + n

    def add_take(self, peer: str, n: int) -> None:
        """Record ``n`` exposures actually taken (also counts them offered)."""
        self.offered[peer] = self.offered.pop(peer, 0) + n  # move to end
        self.taken[peer] = self.taken.get(peer, 0) + n

    def bump_take(self, peer: str) -> None:
        """Count one already-offered exposure as taken (decide-time commit)."""
        self.taken[peer] = self.taken.get(peer, 0) + 1

    def add_recip(  # noqa: PLR0913 -- peer + count + (now, tz) read best flat
        self, peer: str, n: int, now: float, tz: float
    ) -> None:
        """Record ``n`` reciprocations to ``peer`` and the daily counter."""
        stamp = humanize_time.local(now, tz).date().isoformat()
        self.recip_day, self.recip_today = _rolled(
            self.recip_day, self.recip_today, stamp
        )
        self.recip_today += n
        self.recip[peer] = self.recip.get(peer, 0) + n

    def spend_take(self, control: Control, now: float, tz: float) -> bool:
        """Consume one take slot from today's budget; False when capped."""
        stamp = humanize_time.local(now, tz).date().isoformat()
        self.take_day, self.take_today = _rolled(
            self.take_day, self.take_today, stamp
        )
        if control.take_cap > 0 and self.take_today >= control.take_cap:
            return False
        self.take_today += 1
        return True

    def spend_recip(self, control: Control, now: float, tz: float) -> bool:
        """Consume one recip slot from today's budget; False when capped."""
        stamp = humanize_time.local(now, tz).date().isoformat()
        self.recip_day, self.recip_today = _rolled(
            self.recip_day, self.recip_today, stamp
        )
        if control.recip_cap > 0 and self.recip_today >= control.recip_cap:
            return False
        self.recip_today += 1
        return True

    def recip_left(self, control: Control, now: float, tz: float) -> int:
        """Return how many reciprocations are still allowed today (date roll).

        Read-only (does not consume): the story engine plans a batch against
        the remaining budget, then commits with ``add_recip`` when reactions
        actually go out.
        """
        if control.recip_cap <= 0:
            return 0
        stamp = humanize_time.local(now, tz).date().isoformat()
        used = self.recip_today if self.recip_day == stamp else 0
        return max(0, control.recip_cap - used)

    def takes_today(self, now: float, tz: float) -> int:
        """Exposures taken on the local date of ``now`` (else 0)."""
        stamp = humanize_time.local(now, tz).date().isoformat()
        return self.take_today if self.take_day == stamp else 0

    def recips_today(self, now: float, tz: float) -> int:
        """Reciprocations made on the local date of ``now`` (else 0)."""
        stamp = humanize_time.local(now, tz).date().isoformat()
        return self.recip_today if self.recip_day == stamp else 0

    def remember(self, peer: str, label: str) -> None:
        """Cache ``peer``'s display label (@name / title) for /status.

        Ignores an empty label or one that is just the id again, so a failed
        resolution never overwrites a real name already learned.
        """
        if label and label != peer:
            self.names[peer] = label

    def evict(self, peer: str) -> None:
        """Drop a peer's counters and cached name (rolled off the set)."""
        self.offered.pop(peer, None)
        self.taken.pop(peer, None)
        self.recip.pop(peer, None)
        self.names.pop(peer, None)


def warmth(ledger: Ledger, control: Control) -> list[Warmth]:
    """Per-peer attachment readout, MOST RECENT peer first.

    Ordered by recency (the last-interacted-with peer first), not by score, so
    /status shows the people we just engaged rather than an all-time hall of
    fame. ``index`` is the partial Berlyne index exposure(p) * recip(r); a peer
    needs at least one recorded offer to appear. The label is the peer's cached
    display name (``ledger.names``), falling back to the raw id.
    """
    rows: list[Warmth] = []
    for key in reversed(ledger.offered):  # newest interaction first
        offered = ledger.offered[key]
        if offered <= 0:
            continue
        taken = ledger.taken.get(key, 0)
        p = taken / offered
        r = ledger.recip.get(key, 0) / taken if taken else 0.0
        idx = attachment.exposure(p, control.wundt) * attachment.recip(r)
        rows.append(Warmth(ledger.names.get(key, key), p, r, idx, offered))
    return rows
