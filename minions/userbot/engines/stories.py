# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Human-like viewing of friends' and contacts' stories (Berlyne model).

The aggregator's user account also *watches stories*, the way a person idly
would: it opens Telegram now and then, glances at a FRACTION of the newest
unseen stories from people it follows (toward the peak of the Wundt attraction
curve, not all of them), leaves an occasional heart/thumb, and closes the app.
The choices are per-peer so each relationship is steered toward the peak; see
``engines/attachment.py`` for the model.

Like ``reactions.py`` this module is deliberately Telethon-free (pure Python +
stdlib) so every decision is unit-testable; ``main.py`` owns the client, feeds
in the peers who currently have unseen stories (Telegram's own stories feed
already limits that to contacts / people we follow), and calls in here for the
*when* and the *which*.

The human logic, mapped to code:

1. Only UNSEEN stories are ever viewed: ``unseen`` subtracts a persisted
   per-peer seen set, so a re-poll never re-opens a story already watched, and
   we never walk the whole contact list -- we react to what is genuinely new.
2. Viewing happens in SESSIONS: a person opens the app, watches a few, then
   leaves it for a long, heavy-tailed while. ``spacing_*`` is the gap BETWEEN
   sessions; ``gap_*`` is the short gap between two peers INSIDE one, and a
   session views at most ``per_session_*`` peers -- a handful, not everyone.
3. Not everyone gets watched: ``skip_peer_prob`` drops some eligible peers this
   pass, and freshest-first ordering (newest story, least-recently-viewed
   author) means the pile is skimmed, not processed exhaustively.
4. Quiet hours and the odd silent day: no views overnight, and a whole day is
   occasionally skipped, both read from the persona's timezone.
5. State is persisted (the seen set, the session cursor), so a restart keeps
   its memory and never double-counts. What the account DID lives in the
   contact log, and the /status view log is read back out of it.

All human-visible text (none is needed here) would live in the constants JSON;
this source stays ASCII.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from minions.userbot.core import attachment
from minions.userbot.core import codec
from minions.userbot.core import humanize
from minions.userbot.core import relationship
from minions.userbot.core import state as state_store

if TYPE_CHECKING:
    from collections.abc import Callable


def _seen_key(peer_id: int, story_id: int) -> str:
    """Return the dedup key for one story of one peer."""
    return f'{peer_id}:{story_id}'


@dataclass(frozen=True)
class StoryParams:
    """Every tunable, loaded from the constants JSON 'stories' section."""

    enabled: bool = False
    # Catch-up mode: view EVERY unseen story each poll -- no per-session cap,
    # no skip, no long cooldown between sessions -- so all currently-active
    # stories are swept at once instead of a slow human trickle. Quiet hours
    # and the odd silent day still apply, and the per-story dwell / small gap
    # between peers stay (viewing dozens instantly would trip Telegram's flood
    # limits). Off is the human-like handful-then-rest behaviour.
    view_all: bool = False
    # Also view the stories of people whose CHATS were moved to the Archive
    # (Telegram's "hidden" stories feed). Off keeps to the main feed only --
    # archived contacts are left alone. ``main.py`` polls the hidden feed too
    # when this is on; the brain treats both feeds' peers the same.
    include_archived: bool = False
    # The persona's UTC offset: quiet hours / the silent-day date are read in
    # THIS timezone, so the cadence matches the human, not the server clock.
    tz_offset_hours: float = 3.0
    quiet_hours: frozenset[int] = frozenset({1, 2, 3, 4, 5, 6, 7})
    # How often ``main.py`` re-polls the stories feed (test tight, live
    # relaxed; both default via ``poll_sec``). Carried for main, not the brain.
    poll_sec: float = 1800.0
    # One session views between min and max peers -- a glance, not the whole
    # feed. Picked per session so the count itself varies.
    per_session_min: int = 2
    per_session_max: int = 6
    skip_peer_prob: float = 0.35  # chance an eligible peer is skipped
    silent_day_prob: float = 0.06  # chance a whole day views nothing
    # First view lands a short, human "just opened the app" beat after the
    # plan; gap_* is the lognormal pause between one peer and the next in a
    # session; spacing_* is the long, heavy-tailed gap to the NEXT session.
    latency_log_mu: float = 2.5
    latency_log_sigma: float = 0.8
    gap_log_mu: float = 2.3
    gap_log_sigma: float = 0.7
    spacing_log_mu: float = 8.6
    spacing_log_sigma: float = 1.0
    # How long to linger on each individual story (main.py dwells this long
    # between marking one story and the next, so a peer's set is not opened in
    # a single machine-fast blink). Carried for main; the brain does not sleep.
    dwell_min_sec: float = 2.0
    dwell_max_sec: float = 9.0
    max_peers_tracked: int = 500  # cap the persisted seen map (oldest go)
    seen_per_peer: int = 40  # cap the per-peer seen id list (newest kept)
    catch_up_max: int = 12  # peers per poll in view_all (anti-binge)
    # Berlyne exposure control: we view a FRACTION of each peer's stories,
    # toward the Wundt peak (~2/3), not all -- viewing everything sits
    # on the aversion side (reads as stalking). c1/c2/k shape the curve (see
    # engines/attachment.py); its argmax is the per-peer view target, and gain
    # is how hard we correct a peer that is above/below it. Defaults so nothing
    # changes for a caller that does not set them.
    exposure_c1: float = 0.45
    exposure_c2: float = 0.90
    exposure_k: float = 8.0
    view_control_gain: float = 1.0
    # Reciprocity: heart/thumb a FRACTION (react_fraction_target) of the
    # stories we view, so a viewer sees an occasional reaction, not silence --
    # the term that lifts attachment off zero. Capped hard per day (a ban
    # surface); pool kept to a couple of safe standard reactions (escapes keep
    # this file ASCII; the JSON carries the real glyphs). Off = view-only.
    react_enabled: bool = True
    react_fraction_target: float = 0.20
    react_pool: tuple[str, ...] = ('\u270a', '\U0001f44d')
    react_max_per_day: int = 50
    # Two views closer together than this are one sitting, not two visits.
    # Feeds the clumping factor of the attachment model (relationship.py):
    # attention that all arrives at once reaches tedium sooner than the same
    # amount spread out. Well under spacing_log_mu, which is the gap BETWEEN
    # sessions, so a real second visit is never mistaken for the same one.
    burst_gap_sec: float = 900.0
    # The individual curve this person is walked around: honeymoon,
    # cold shoulder, then an unpredictable swing between the two. Shared
    # with the like engine through the persona block, because it is one
    # person's attention. Built by relationship.load_arc, not by the
    # codec, since it is a list of objects rather than a scalar.
    arc: relationship.Arc = relationship.NO_ARC


@dataclass(frozen=True)
class StoryCandidate:
    """One peer that currently has active stories, as seen by ``main.py``.

    ``story_ids`` are all the peer's active story ids; ``unseen`` is derived by
    subtracting our persisted seen set. ``max_id`` is the highest active id (so
    a single ``ReadStories`` up to it marks the peer fully read). ``last_ts``
    is the newest story's unix time, used only for freshest-first ordering.
    """

    peer_id: int
    story_ids: tuple[int, ...]
    max_id: int
    last_ts: float = 0.0
    hidden: bool = False  # came from the archived feed, not the main one


VIEWING = 'viewing'
PASSED = 'passed this glance'
NOTHING_NEW = 'nothing new'
"""What a glance can conclude about one peer who has stories up."""


def _verdict(unseen: int, viewing: int, blocked: str) -> str:
    """Return why one peer is, or is not, being opened this glance."""
    if viewing:
        return VIEWING
    if not unseen:
        return NOTHING_NEW
    return blocked or PASSED


@dataclass(frozen=True)
class Standing:
    """One peer's all-time record with us, as of this glance.

    ``offered`` at zero means we have never engaged them at all -- a
    first sighting, which reads very differently from a peer we have
    deliberately been skipping, and the two used to look identical.
    """

    offered: int = 0
    viewed: int = 0
    reacted: int = 0


@dataclass(frozen=True)
class Seen:
    """One peer with active stories, and what the last glance decided.

    The operator can see who has stories in Telegram; without this they
    cannot see what the bot decided about them, which makes an idle
    queue indistinguishable from an ignored person.
    """

    peer_id: int
    active: int = 0  # stories they have up right now
    unseen: int = 0  # of those, ones we have never opened
    viewing: int = 0  # how many we will open this glance
    verdict: str = NOTHING_NEW
    hidden: bool = False  # from the archived feed, not the main one
    standing: Standing = field(default_factory=Standing)


@dataclass(frozen=True)
class Glance:
    """What the last poll saw and decided -- a readout, never state.

    In memory only: it describes one pass and is worthless after a
    restart, so it has no business in the state file. ``at`` is what
    lets a report say how stale it is, which matters because /status
    must not re-read the feed just to look current.
    """

    at: float = 0.0
    peers: tuple[Seen, ...] = ()
    blocked: str = ''  # why nothing was planned at all, if so


@dataclass(frozen=True)
class StoryView:
    """A planned view: watch ``story_ids`` of ``peer_id`` at ``when``.

    The exact unseen ids are decided when the session is planned (not at view
    time), so the queue is inspectable and a failed read does not silently mark
    a peer seen. ``main.py`` sleeps until ``when``, opens each story, then
    marks the peer read up to ``max_id`` and calls ``mark_viewed``.
    """

    peer_id: int
    story_ids: tuple[int, ...]
    max_id: int
    when: float
    # The subset of story_ids to react to, and the reaction glyph chosen for
    # this view (empty when none) -- decided at plan time, sent by the glue.
    react_ids: tuple[int, ...] = ()
    react_emoji: str = ''


@dataclass(frozen=True)
class ViewLog:
    """One line of the rolling view log, shown by /status and /stories.

    No name: who this peer is lives once, in ``actors``, and the render
    looks it up. A log line used to carry its own copy, which meant a peer
    who changed their @name read as two different people down the list.
    """

    peer_id: int
    count: int
    ts: float


@dataclass
class StoryState:
    """The engine's cursors -- six scalars, and nothing else.

    Everything that was a collection has left: the seen story ids are marks
    (principle 1, never re-view), the per-peer ledger is rows in
    ``standing``, and the rolling view log is DERIVED from ``contact`` now
    rather than kept beside it. A blob is rewritten whole on every touch, so
    a collection in here costs its whole length per write; and the view log
    additionally disagreed with the counters as soon as its cap threw a row
    away.
    """

    last_view: float = 0.0
    next_session_at: float = 0.0
    session_start_at: float = 0.0
    session_last_at: float = 0.0
    total_views: int = 0  # stories viewed all-time (a simple odometer)
    last_react: float = 0.0  # ts of the last reaction, for the min-gap pacing


class StoryBrain:
    """The stateful engine: pick which unseen stories to view, and when.

    ``rng`` is injected so tests are deterministic; production uses a
    seeded-at-start ``random.Random``. Tests that need a fixed clock assign
    ``brain.clock`` (a ``() -> float``) after construction.
    """

    clock: Callable[[], float]

    def __init__(
        self,
        params: StoryParams,
        store: state_store.StateStore,
        rng: random.Random | None = None,
    ) -> None:
        """Bind the params and the shared store; seed the RNG."""
        self.params = params
        self.store = store
        self.rng = rng or random.Random()  # noqa: S311 -- mimicry, not crypto
        self.clock = time.time
        self.ledger = relationship.Ledger(store)
        self.state = self._load()
        self.last_glance = Glance()

    def unseen(self, cand: StoryCandidate) -> tuple[int, ...]:
        """Return the candidate's story ids we have not viewed yet."""
        return tuple(
            sid
            for sid in cand.story_ids
            if not self.store.marked(_seen_key(cand.peer_id, sid))
        )

    def plan(
        self, candidates: list[StoryCandidate], now: float | None = None
    ) -> list[StoryView]:
        """Plan this pass's views: a human glance, or nothing.

        Returns an empty list -- view nothing -- when disabled, in quiet hours,
        on a silent day, or still inside the between-session cooldown.
        Otherwise picks a small, freshest-first, partly-skipped subset of the
        peers with unseen stories, staggered across a short viewing session.
        Marks nothing seen; ``mark_viewed`` does that once a read succeeds.

        Whatever it decides is also recorded in ``last_glance``, so /status
        can say who we are opening and who we are not -- the decision is
        made here and was previously thrown away.
        """
        now = self.clock() if now is None else now
        blocked = self.blocked_reason(now) or ''
        views = [] if blocked else self._plan_views(candidates, now)
        self.last_glance = Glance(
            at=now,
            peers=self._seen_rows(candidates, views, blocked),
            blocked=blocked,
        )
        return views

    def _plan_views(
        self, candidates: list[StoryCandidate], now: float
    ) -> list[StoryView]:
        """Choose and lay out this session's views (the session is open)."""
        eligible = self._eligible(candidates)
        if not eligible:
            return []
        # Catch-up: view every unseen peer this poll. Otherwise a human glance:
        # a capped, partly-skipped handful.
        # view_all catches up on the backlog, but still capped per poll so a
        # night's unseen stories are not swept in one machine-fast morning
        # binge -- the freshest catch_up_max now, the rest next poll.
        chosen = (
            eligible[: self.params.catch_up_max]
            if self.params.view_all
            else self._pick_peers(eligible)
        )
        return self._lay_out(chosen, now) if chosen else []

    def _seen_rows(
        self,
        candidates: list[StoryCandidate],
        views: list[StoryView],
        blocked: str,
    ) -> tuple[Seen, ...]:
        """Describe every peer who has stories up, and our verdict on each.

        Every candidate lands in exactly one bucket. The verdict is the
        honest reason we are not opening someone: nothing we have not
        already seen, passed this glance, or the whole session blocked
        (quiet hours, cooldown, silent day).
        """
        opening = {view.peer_id: len(view.story_ids) for view in views}
        return tuple(
            Seen(
                peer_id=cand.peer_id,
                active=len(cand.story_ids),
                unseen=(unseen := len(self.unseen(cand))),
                viewing=(count := opening.get(cand.peer_id, 0)),
                verdict=_verdict(unseen, count, blocked),
                hidden=cand.hidden,
                standing=self._standing(cand.peer_id),
            )
            for cand in candidates
        )

    def _standing(self, peer_id: int) -> Standing:
        """Return one peer's all-time record, for the glance readout."""
        row = self.ledger.row(peer_id)
        return Standing(row.offered, row.taken, row.recip)

    def blocked_reason(self, now: float | None = None) -> str | None:
        """Return why no session may open now, or None when one may.

        A short, log-friendly diagnostic ('disabled', 'quiet-hours',
        'silent-day', 'cooldown Ns') so an empty queue is explainable instead
        of mysterious.
        """
        now = self.clock() if now is None else now
        tz = self.params.tz_offset_hours
        if not self.params.enabled:
            return 'disabled'
        if humanize.in_quiet_hours(now, tz, self.params.quiet_hours):
            return 'quiet-hours'
        if humanize.is_silent_day(now, tz, self.params.silent_day_prob):
            return 'silent-day'
        wait = self.state.next_session_at - now
        if wait > 0:
            # catch-up (view_all) ALSO waits between sessions -- it clears the
            # backlog a session's worth at a time at the normal human rhythm,
            # NOT a fast drain every poll, so a night's stories do not all land
            # in one morning burst.
            return f'cooldown {int(wait)}s'
        return None

    def _eligible(
        self, candidates: list[StoryCandidate]
    ) -> list[tuple[StoryCandidate, tuple[int, ...]]]:
        """Return (candidate, unseen ids) pairs that still have unseen stories.

        Freshest first (newest story leads), so a glance skims the top of the
        pile the way a person does -- not a stable walk down the contact list.
        """
        pairs = [
            (cand, unseen)
            for cand in candidates
            if (unseen := self.unseen(cand))
        ]
        pairs.sort(key=lambda p: p[0].last_ts, reverse=True)
        return pairs

    def _pick_peers(
        self, eligible: list[tuple[StoryCandidate, tuple[int, ...]]]
    ) -> list[tuple[StoryCandidate, tuple[int, ...]]]:
        """Take a capped, partly-skipped slice of the eligible peers.

        A person does not open every story: some are skipped this sitting
        (``skip_peer_prob``), and only up to a per-session cap are viewed at
        all. Freshest peers are offered first, so a skip pushes attention down
        the pile rather than dropping the newest.
        """
        cap = self._session_cap()
        chosen: list[tuple[StoryCandidate, tuple[int, ...]]] = []
        for pair in eligible:
            if len(chosen) >= cap:
                break
            if self.rng.random() < self.params.skip_peer_prob:
                continue
            chosen.append(pair)
        # If skipping emptied the glance but there was something to see, still
        # view the single freshest peer -- a person who opened the app watches
        # at least one, they do not open it and immediately close it.
        if not chosen and eligible:
            chosen.append(eligible[0])
        return chosen

    def _session_cap(self) -> int:
        """Return how many peers THIS session may view (min..max, min 1)."""
        lo = max(1, self.params.per_session_min)
        hi = max(lo, self.params.per_session_max)
        return self.rng.randint(lo, hi)

    def _control(self) -> relationship.Control:
        """Build the shared Berlyne control from this engine's params.

        Views are uncapped (take_cap=0); reactions carry the daily cap.
        """
        p = self.params
        return relationship.Control(
            wundt=attachment.WundtParams(
                c1=p.exposure_c1, c2=p.exposure_c2, k=p.exposure_k
            ),
            take_gain=p.view_control_gain,
            recip_target=p.react_fraction_target,
            take_cap=0,
            recip_cap=p.react_max_per_day,
            burst_gap_sec=p.burst_gap_sec,
            arc=p.arc,
        )

    def _tz(self) -> float:
        """Return the persona timezone offset (for the daily counters)."""
        return self.params.tz_offset_hours

    def _view_target(self, peer_id: int = 0, now: float = 0.0) -> float:
        """Return the view fraction to steer THIS peer toward right now.

        The Wundt peak, scaled by whichever leg of their own arc they are in
        -- so two people watched in the same session can be aimed at very
        different fractions, which is the whole point of a curve per person.
        With no arc configured it is the peak for everybody, forever.
        """
        ctrl = self._control()
        return ctrl.take_target(self.ledger.leg(peer_id, ctrl, now))

    def _view_split(
        self, peer_id: int, unseen: tuple[int, ...], p_star: float
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        """Split a peer's unseen ids into (view, skip), steering p -> p_star.

        Per story a Bernoulli draw with the shared corrective probability, so
        the running ``viewed/offered`` converges on the Wundt peak: an
        over-viewed peer is skipped harder, an under-viewed one viewed more.
        Runs on local counters (the ledger commits at ``mark_viewed`` /
        ``_record_skips``, so the plan/commit split survives I/O that fails).
        """
        row = self.ledger.row(peer_id)
        offered, viewed = row.offered, row.taken
        gain = self.params.view_control_gain
        view_ids: list[int] = []
        skip_ids: list[int] = []
        for sid in unseen:
            p_cur = viewed / offered if offered else p_star
            if self.rng.random() < relationship.steer(p_cur, p_star, gain):
                view_ids.append(sid)
                viewed += 1
            else:
                skip_ids.append(sid)
            offered += 1
        return tuple(view_ids), tuple(skip_ids)

    def _record_skips(
        self, peer_id: int, skip_ids: tuple[int, ...], now: float = 0.0
    ) -> None:
        """Mark deliberately-skipped stories seen (offered, never viewed).

        Decided once at plan time so a skipped story is not re-offered every
        poll (which would drive p back to 1); it counts as offered, not
        viewed.

        ``now`` is the session's moment, the same one ``mark_viewed`` is
        given, so an ignore and a seen decided in one glance are stamped
        alike. Reading the clock here instead put every ignore at plan time
        while the views landed minutes later as the session staggered them --
        and the arc anchors on the FIRST contact row, so under an injected
        clock the two disagreeing left the whole curve measured from the
        wrong day.
        """
        fresh = [
            sid for sid in skip_ids if self.store.mark(_seen_key(peer_id, sid))
        ]
        if not fresh:
            return
        self.ledger.add_offer(peer_id, tuple(fresh), now or self.clock())
        self._trim_peers()

    def _react_budget(self, now: float) -> int:
        """Return how many reactions are still allowed today (date roll)."""
        if not self.params.react_enabled:
            return 0
        return self.ledger.recip_left(self._control(), now, self._tz())

    def _plan_reacts(  # noqa: PLR0913 -- peer + what + (budget, now) read flat
        self,
        peer_id: int,
        view_ids: tuple[int, ...],
        budget: int,
        now: float = 0.0,
    ) -> tuple[tuple[int, ...], int]:
        """Pick which viewed ids to react to, steering recip/taken -> target.

        The shared reciprocity control: a per-story Bernoulli at the corrective
        probability (so reacted/viewed converges on ``react_fraction_target``),
        stopped by the remaining daily ``budget``. Returns the chosen ids and
        the budget left.
        """
        key = peer_id
        ctrl = self._control()
        chosen: list[int] = []
        for sid in view_ids:
            if budget <= 0:
                break
            prob = self.ledger.recip_prob(key, ctrl, now, taken_now=False)
            if self.rng.random() < prob:
                chosen.append(sid)
                budget -= 1
        return tuple(chosen), budget

    def _lay_out(
        self,
        chosen: list[tuple[StoryCandidate, tuple[int, ...]]],
        now: float,
    ) -> list[StoryView]:
        """Stagger the chosen peers across one session and set the cursors.

        Each peer's unseen ids are split toward the Wundt view fraction: only
        the chosen subset becomes a view (the skipped rest is recorded seen so
        it is not re-offered). The first view lands a short "just opened the
        app" beat from now; each next peer follows a lognormal gap later; the
        between-session cursor is pushed a long, heavy-tailed spacing past the
        last view (principle 2).
        """
        budget = self._react_budget(now)
        when = now + humanize.lognormal(
            self.rng, self.params.latency_log_mu, self.params.latency_log_sigma
        )
        self.state.session_start_at = when
        views: list[StoryView] = []
        for cand, unseen in chosen:
            view_ids, skip_ids = self._view_split(
                cand.peer_id, unseen, self._view_target(cand.peer_id, now)
            )
            self._record_skips(cand.peer_id, skip_ids, now)
            if not view_ids:
                continue  # this peer was skipped entirely this pass
            react_ids, budget = self._plan_reacts(
                cand.peer_id, view_ids, budget, now
            )
            pool = self.params.react_pool
            emoji = self.rng.choice(pool) if react_ids and pool else ''
            views.append(
                StoryView(
                    peer_id=cand.peer_id,
                    story_ids=view_ids,
                    max_id=max(view_ids),
                    when=when,
                    react_ids=react_ids,
                    react_emoji=emoji,
                )
            )
            when += humanize.lognormal(
                self.rng, self.params.gap_log_mu, self.params.gap_log_sigma
            )
        self.state.session_last_at = when
        # The between-session cursor is pushed a heavy-tailed spacing past the
        # last view -- for catch-up (view_all) TOO, so a backlog is cleared a
        # session's worth at a time at the human rhythm rather than swept every
        # poll (which dumped a whole night's stories in one morning burst).
        spacing = humanize.lognormal(
            self.rng,
            self.params.spacing_log_mu,
            self.params.spacing_log_sigma,
        )
        self.state.next_session_at = when + spacing
        self._save()
        return views

    def mark_viewed(
        self,
        peer_id: int,
        story_ids: tuple[int, ...],
        ts: float | None = None,
    ) -> None:
        """Record that ``story_ids`` of ``peer_id`` were viewed (persisted).

        Idempotent: re-marking an already-seen id changes nothing. Keeps the
        per-peer seen list and the tracked-peer map bounded, appends one line
        to the rolling view log, and advances the odometer by the number of
        newly-seen ids (so a re-mark does not inflate the count).
        """
        now = self.clock() if ts is None else ts
        fresh = [
            sid
            for sid in story_ids
            if self.store.mark(_seen_key(peer_id, sid))
        ]
        if not fresh:
            return
        self.ledger.add_take(peer_id, tuple(fresh), self._control(), now)
        self.store.trim_marks(f'{peer_id}:', self.params.seen_per_peer)
        self._trim_peers()
        self.state.last_view = now
        self.state.total_views += len(fresh)
        self._save()

    def mark_reacted(
        self, peer_id: int, story_ids: tuple[int, ...], now: float
    ) -> None:
        """Record the reactions sent to ``peer_id`` (persisted).

        Rolls the per-day counter over at local midnight, bumps the per-peer
        reaction tally (so r = reacted/viewed stays true), and stamps the last
        reaction time. Called by the glue after the reactions actually go out,
        with the ids they landed on -- so the log says which story got the
        heart, not merely that one did.
        """
        if not story_ids:
            return
        self.ledger.add_recip(peer_id, story_ids, now, self._tz())
        self.state.last_react = now
        self._save()

    def _trim_peers(self) -> None:
        """Keep only the most recent ``max_peers_tracked`` peers.

        Recency is a column now, so the store does the ordering; we clear the
        seen marks the dropped peers keyed, since the store does not know
        their format.
        """
        for peer in self.store.trim_peers(self.params.max_peers_tracked):
            self.store.drop_marks(f'{peer}:')

    def recent_log(self, limit: int) -> list[ViewLog]:
        """Return recent SITTINGS, newest first (for /status, /stories).

        Derived from the contact log rather than kept beside it. It used to
        be a rolling list inside the JSON blob, capped at 50 rows -- a
        second recording of acts already written, which began disagreeing
        with the counters the moment the cap threw one away.
        """
        return [
            ViewLog(int(r['peer_id']), int(r['n']), float(r['ts']))
            for r in self.store.glances(
                state_store.ACTS['stories'][1],
                self.params.burst_gap_sec,
                limit,
            )
        ]

    def seen_count(self) -> int:
        """Return how many stories have been viewed all-time (the odometer)."""
        return self.state.total_views

    def reacts_today(self, now: float, tz: float) -> int:
        """Return reactions sent on the local date of ``now`` (0 past it)."""
        return self.ledger.recips_today(now, tz)

    def warmth(self) -> list[relationship.Warmth]:
        """Per-peer attachment readout for /status, most recent first."""
        return relationship.warmth(self.ledger, self._control())

    def views_today(self, now: float, tz: float) -> int:
        """Return how many stories were viewed on the local date of ``now``.

        Counted straight off the contact log, so it resets naturally at
        local midnight -- what /status shows instead of the all-time
        odometer -- and it no longer stops at whatever the rolling blob log
        happened to still be holding.
        """
        today = humanize.local(now, tz).date()
        return sum(
            1
            for act in self.store.acts_since(
                state_store.ACTS['stories'][1], now - 2 * 86400.0
            )
            if humanize.local(act, tz).date() == today
        )

    def _load(self) -> StoryState:
        """Reload the state block, or start fresh when there is none."""
        raw = self.store.read()
        self.ledger.restore(raw)
        return StoryState(
            last_view=codec.num(raw.get('last_view')),
            next_session_at=codec.num(raw.get('next_at')),
            session_start_at=codec.num(raw.get('start_at')),
            session_last_at=codec.num(raw.get('last_at')),
            total_views=codec.whole(raw.get('total_views')),
            last_react=codec.num(raw.get('last_react')),
        )

    def _save(self) -> None:
        """Publish this engine's state block -- SCALARS ONLY.

        The rolling view log and the nested ``session`` object are both gone
        from here: one was a second copy of ``contact``, the other was three
        scalars wearing a dict.
        """
        self.store.write(
            {
                'last_view': self.state.last_view,
                'next_at': self.state.next_session_at,
                'start_at': self.state.session_start_at,
                'last_at': self.state.session_last_at,
                'total_views': self.state.total_views,
                'last_react': self.state.last_react,
                **self.ledger.counters(),
            },
        )


def load_story_params(
    data: dict[str, object], mode: str = 'live'
) -> StoryParams:
    """Load the stories engine's parameters from the JSON 'stories' key.

    Every knob reads its own key and falls back to its own declared default
    (``core/codec.py``). Two are spelled out: the re-poll cadence, which is
    per profile (test tight, live relaxed), mirroring the reactions rescan
    interval, and both fall back to ``poll_sec``; and the arc, which is a
    list of objects rather than a scalar, so it is built by its own loader.
    """
    cfg = codec.engine(data, 'stories')
    poll_key = 'poll_sec_test' if mode == 'test' else 'poll_sec_live'
    default_poll = codec.num(cfg.get('poll_sec'), StoryParams.poll_sec)
    return codec.decode(
        StoryParams,
        cfg,
        {
            'poll_sec': codec.num(cfg.get(poll_key), default_poll),
            'arc': relationship.load_arc(cfg),
        },
    )
