# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Human-like reaction-emoji reactions to people who comment on the last posts.

The aggregator posts announces; this module lets its user account react to a
commenter's comment with a premium reaction emoji ONCE, timed and chosen so the
behaviour reads as a distracted human, not a scheduler. It is deliberately
Telethon-free (pure Python + stdlib) so every decision is unit-testable;
``main.py`` owns the client and calls in here for the *when* and the *what*.

The nine principles, mapped to code:

1. Timing is a distribution, not uniform: ``_density_weight`` is a mixture of
   Gaussians over the day (separate weekday/weekend curves), multiplied by how
   likely the host is actually up then -- a curve LEARNED from real uptime
   (``mark_alive``) blended with the declared window -- so the schedule adapts
   to any NAS on-time, not just the rule of thumb.
2. Answering happens in SESSIONS: a human opens the comments now and then,
   clears the pile in a quick burst (short intra-session gaps), then closes the
   app for a long, heavy-tailed while. ``spacing_*`` is the gap BETWEEN
   sessions; ``session_gap_*`` is the gap between reactions INSIDE one -- so
   bursts
   then silence, and co-occurring comments land in the same burst.
3. Selection has memory: weight = base preference * recency penalty (an
   exponential recovery from the last time that reaction was used), so
   favourites
   lead and just-used reactions fade.
4. A latent "mood" does an AR(1) random walk day to day and tilts selection
   toward sleepy vs. lively reactions -- day-to-day coherence, not
   memorylessness.
5. Context tags (daypart, season, holiday) re-weight the pool: a sleepy
reaction in
   the morning, a festive one in December.
6. Jitter defeats the ":00 scheduler fingerprint": the fire time gets a random
   sub-minutes offset.
7. Built-in imperfection: a comment is sometimes ignored and a whole day is
   sometimes silent.
8. Feedback reactivity: a commenter who is themselves replying to us gets a
   faster reaction.
9. State is persisted (mood, the session cursors, per-reaction recency, who
   was already reacted), because principles 2-4 need memory across restarts.

All texts/ids live in the constants JSON, so this source stays ASCII.
"""

from __future__ import annotations

import json
import math
import random
import time
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from typing import TYPE_CHECKING

from minions.userbot.core import attachment
from minions.userbot.core import codec
from minions.userbot.core import config
from minions.userbot.core import humanize
from minions.userbot.core import relationship
from minions.userbot.core import state as state_store
from minions.userbot.core.humanize import recency_penalty
from minions.userbot.core.humanize import weighted_choice

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from datetime import datetime

    from minions.userbot.core.models import Emoji

# When a session start lands in a dead hour we sample a real send moment on
# this grid across the reachable awake time (see _place); 5 min is fine-grained
# enough to spread a morning burst without leaving a HH:00 fingerprint.
_PLACE_STEP_SEC = 300.0
# One observation added to the current hour bucket per heartbeat (mark_alive).
_ALIVE_STEP = 1.0
# A comment only joins an already-planned session if that session is within
# reach of NOW. A session forced far ahead (the cursor landed past the window
# edge, so the next session opens next morning) must NOT vacuum up this
# evening's comments -- else a whole evening batch stampedes the 7:00 edge.
_SESSION_REACH_SEC = 7200.0
# A persisted emoji row is an [id, fallback] pair.
_EMOJI_ROW_LEN = 2
# datetime.weekday(): Monday is 0, so Saturday (5) and up is the weekend.
_SATURDAY = 5
# Local hours: midnight..noon reads sleepy, noon..midnight bodry.
_NOON = 12
# Calendar month numbers used as context switches.
_DECEMBER = 12


@dataclass(frozen=True)
class Reaction:
    """A scheduled reaction: react to ``reply_to`` in ``chat``.

    ``reply_to`` is the commenter's message id -- the reaction emoji reaction
    is
    placed directly on it (not a reply in the thread). ``root`` is the post
    (thread root) the comment sits under; it is kept for the
    once-per-(post, person) dedup key and the /status readout, not for
    placement (a reaction needs no threading). For a plain group it equals
    ``reply_to``.

    ``emojis`` is the exact reaction(s) chosen for THIS reaction, decided when
    the
    comment is scheduled (not at send time), so the queue is deterministic and
    inspectable: /status and /requeue can show which reaction lands where. Each
    item is an ``(emoji_id, fallback)`` pair. Today the pickers return exactly
    one; the tuple stays because it is the persisted shape, and collapsing it
    would be a state migration for no gain.
    """

    chat: int
    reply_to: int  # the commenter's message id
    root: int  # the thread root (post) for discussion threading
    when: float
    text: str = ''  # a snippet of the comment being answered (for status)
    emojis: tuple[
        tuple[str, str], ...
    ] = ()  # the chosen (id, fallback) reactions
    kind: str = 'react'  # 'react' = a like reaction; 'reply' = thread sticker


def _emojis_from_row(raw: object) -> tuple[tuple[str, str], ...]:
    """Parse a persisted ``emojis`` list of [id, fallback] pairs."""
    rows = raw if isinstance(raw, list) else []
    return tuple(
        (str(row[0]), str(row[1]))
        for row in rows
        if isinstance(row, (list, tuple)) and len(row) == _EMOJI_ROW_LEN
    )


def _queued(row: sqlite3.Row) -> Reaction:
    """Rebuild one queued Reaction from its row in ``scheduled``.

    Every field is a column now except ``emojis``, which stays JSON on
    purpose: it is the chosen (id, fallback) pairs for THIS one reaction --
    a composite value belonging to the row, not a collection of entities
    that anything would ever query across.
    """
    return Reaction(
        chat=int(row['chat']),
        reply_to=int(row['reply_to']),
        root=int(row['root']),
        when=float(row['due_at']),
        text=str(row['text']),
        emojis=_emojis_from_row(json.loads(str(row['emojis']))),
        kind=str(row['kind']),
    )


def _queue_row(reaction: Reaction) -> tuple[object, ...]:
    """Return one queued Reaction as the columns ``enqueue`` writes."""
    return (
        reaction.chat,
        reaction.reply_to,
        reaction.root,
        reaction.when,
        reaction.kind,
        reaction.text,
        json.dumps([[eid, fb] for eid, fb in reaction.emojis]),
    )


@dataclass(frozen=True)
class ReactionParams:
    """Every tunable, loaded from the constants JSON 'reactions' section."""

    enabled: bool = False
    # Bypass the human-like gates (skip_prob, silent days, the asleep/stale
    # drop) so every comment reaches the send step. It does NOT get the last
    # word: with attach_enabled on, the exposure control still declines some
    # of them, so "absolutely every comment" holds only with attach off. Dedup
    # and the manual-reply check apply either way.
    like_all: bool = False
    # When the target is a CHANNEL, its comments live in the linked discussion
    # group. True: resolve each post's discussion thread and react only there
    # (so reactions land in the channel post's comments). False: match replies
    # to the post id directly (the target is a plain group).
    comments_in_discussion: bool = False
    # Whether to also drop a reaction on our OWN fresh posts (immediately, no
    # human-like wait). Optional and off by default: the engine's job is
    # reacting to COMMENTERS; liking our own posts is a separate extra.
    react_to_posts: bool = False
    watch_posts: int = 4
    hours_weekday: tuple[tuple[float, float, float], ...] = (
        (9.0, 2.0, 1.0),
        (21.0, 2.5, 1.3),
    )
    hours_weekend: tuple[tuple[float, float, float], ...] = (
        (11.0, 3.0, 1.0),
        (22.0, 3.0, 1.2),
    )
    quiet_hours: frozenset[int] = frozenset({2, 3, 4, 5, 6})
    # The DECLARED uptime window in local hours [start, end) -- a prior only.
    # The engine also LEARNS the real on-hours (mark_alive), and blends the two
    # by confidence, so it adapts to whatever hours the NAS is actually up, not
    # just this rule of thumb. start >= end (or 0/24) means "up all day".
    # (JSON keys: active_start_hour / active_end_hour.)
    active_start: float = 0.0
    active_end: float = 24.0
    # How the learned uptime is weighed: uptime_half_life_sec fades old
    # observations (so a changed schedule is followed), uptime_learn_obs is how
    # many heartbeats of history earn full trust in the learned curve over the
    # declared window. skip_if_manually_replied drops the reaction when the
    # operator has already replied to that comment by hand.
    uptime_half_life_sec: float = 864000.0
    uptime_learn_obs: float = 2000.0
    skip_if_manually_replied: bool = True
    # The persona's UTC offset: hours/dates are read in THIS timezone, so the
    # cadence matches the legend, not the server's clock (principle 9).
    tz_offset_hours: float = 3.0
    latency_log_mu: float = 7.0
    latency_log_sigma: float = 1.2
    spacing_log_mu: float = 9.5
    spacing_log_sigma: float = 1.3
    jitter_sec: float = 90.0
    skip_prob: float = 0.12
    silent_day_prob: float = 0.08
    recency_half_life_sec: float = 172800.0
    mood_phi: float = 0.8
    mood_sigma: float = 0.35
    feedback_speedup: float = 0.4
    # Session model (see ReactionBrain._plan): spacing_* is the long gap
    # BETWEEN comment-answering sessions; the fields below shape ONE session.
    session_gap_log_mu: float = 3.8  # log-mean of the intra-session gap
    session_gap_log_sigma: float = 0.6
    session_idle_sec: float = 900.0  # a longer silence ends the session
    session_max_sec: float = 1200.0  # hard cap on one session's span
    max_reply_delay_sec: float = 21600.0  # older than this -> too stale
    pool: tuple[Emoji, ...] = ()
    # The LIKE pool: the emoji placed as the default reaction. Chosen
    # pseudo-randomly but DETERMINISTICALLY -- seeded by the target id, so the
    # same comment/post always yields the same like: varied across targets, yet
    # recomputable after a restart (no persisted cursor). Separate from the
    # reaction ``pool``.
    like_pool: tuple[Emoji, ...] = ()
    # How often (seconds) the bot re-scans the targets on its own -- picking up
    # posts created (and commented on) while it runs, without waiting for a
    # restart or a manual /requeue. 0 turns the auto-rescan off.
    rescan_sec: float = 300.0
    # --- Berlyne attachment control (per commenter) -----------------------
    # When on, we do NOT like every comment. Instead the fraction of a
    # person's comments we engage (like or sticker) is steered toward the
    # Wundt peak (~0.67): liking everything reads as desperate, so a heavy
    # commenter is throttled while a newcomer is still acknowledged. This
    # decides LAST, so it wins over like_all; turning it off is what makes
    # like_all mean what it says. The like reaction is the EXPOSURE act; the
    # thread sticker is the RECIPROCITY act.
    attach_enabled: bool = True
    # The Wundt exposure curve params (engines/attachment.py): their argmax IS
    # the like-fraction target -- there is no separate 0.67 constant.
    exposure_c1: float = 0.45
    exposure_c2: float = 0.90
    exposure_k: float = 8.0
    # Feedback gain of the exposure control: how hard an over/under-liked
    # commenter is corrected back toward the peak.
    like_control_gain: float = 1.0
    # Reciprocity target: among the comments we engage, the fraction upgraded
    # from a plain like to the stronger thread STICKER (r = stickered/engaged).
    recip_fraction_target: float = 0.20
    recip_control_gain: float = 1.0
    # Ban-surface caps (persona tz, date-keyed). like_max_per_day caps total
    # engagements a day (the like reaction dominates); sticker_max_per_day caps
    # the message-shaped stickers, the real ban surface. 0 disables the cap.
    like_max_per_day: int = 400
    sticker_max_per_day: int = 40
    # The individual curve this person is walked around: honeymoon, cold
    # shoulder, then an unpredictable swing between the two. Shared with
    # the story engine through the persona block, because it is ONE
    # person's attention -- warming to somebody on stories while going
    # cold on their comments is not a mood swing, it is two people.
    arc: relationship.Arc = relationship.NO_ARC
    # Optional persona nickname for this engine (e.g. "cat"). When set,
    # the neutral reaction commands ALSO answer under it -- /<label>now and
    # /<label>_on|off|test|live -- so the persona keeps its own vocabulary
    # without hard-coding it. Empty (default) = neutral commands only.
    label: str = ''


@dataclass
class ReactionState:
    """The engine's cursors: everything bounded, nothing per-audience.

    What used to sit here and grow with the audience now lives in the state
    store: the per-commenter ledger as rows, and the once-per-(post, person)
    dedup as marks. What is left is a handful of scalars plus three bounded
    lists -- the watched posts (four), the due queue (tens), and the
    per-emoji recency of a 39-entry catalog. That is the whole reason this
    is still a readable JSON block.
    """

    mood: float = 0.0
    mood_day: str = ''  # ISO date of the last mood step (drift once a day)
    next_session_at: float = (
        0.0  # earliest the NEXT session may open (spacing)
    )
    session_start_at: float = 0.0  # when the current burst began
    session_last_at: float = 0.0  # last reaction placed in the current burst
    # The three collections below are WORKING COPIES of their own tables --
    # the blob holds scalars only, so each is loaded from and written to
    # `scheduled`, `watching` and `emoji_used` rather than serialised here.
    # They stay in memory because each is small and read per decision.
    reaction_last: dict[str, float] = field(
        default_factory=dict
    )  # id -> last ts
    posts: list[tuple[int, int]] = field(default_factory=list)  # comment tgts
    pending: list[Reaction] = field(default_factory=list)  # due reactions


def _local(ts: float, params: ReactionParams) -> datetime:
    """``ts`` as a datetime in the persona's timezone (principle 9)."""
    return humanize.local(ts, params.tz_offset_hours)


def _mixture(
    hour: float, peaks: tuple[tuple[float, float, float], ...]
) -> float:
    """Sum of Gaussian bumps at ``hour`` -- the day's activity density."""
    total = 0.0
    for mean, sigma, weight in peaks:
        total += weight * math.exp(-0.5 * ((hour - mean) / sigma) ** 2)
    return total


def _in_window(hour: float, params: ReactionParams) -> bool:
    """Whether ``hour`` is inside the host's uptime window [start, end)."""
    if params.active_start >= params.active_end:
        return True  # no window configured -> always up
    return params.active_start <= hour < params.active_end


def _density_weight(ts: float, params: ReactionParams) -> float:
    """Return the day's activity density at ``ts`` (principle 1), 0 if quiet.

    This is the *shape* of a waking day; whether the host is actually up at
    that hour is a separate factor (the observed-uptime multiplier), so the
    schedule adapts to any NAS on-time, not just the declared window.
    """
    when = _local(ts, params)
    if when.hour in params.quiet_hours:
        return 0.0
    weekend = when.weekday() >= _SATURDAY
    peaks = params.hours_weekday if not weekend else params.hours_weekend
    return _mixture(when.hour + when.minute / 60.0, peaks)


def _lognormal(rng: random.Random, mu: float, sigma: float) -> float:
    """Return a heavy-tailed positive draw (principle 2): exp of a normal."""
    return humanize.lognormal(rng, mu, sigma)


def _jitter(ts: float, params: ReactionParams, rng: random.Random) -> float:
    """Add a random sub-minutes offset so timestamps are not on the :00.

    Principle 6: a scheduled task firing on the exact minute is a fingerprint.
    """
    return ts + rng.uniform(0.0, params.jitter_sec)


def _is_silent_day(ts: float, params: ReactionParams) -> bool:
    """Whether the whole day at ``ts`` is a silent one (principle 7).

    Deterministic per date (seeded by the date) so a restart does not flip a
    day that was already decided.
    """
    return humanize.is_silent_day(
        ts, params.tz_offset_hours, params.silent_day_prob
    )


def _context_tags(ts: float, params: ReactionParams) -> frozenset[str]:
    """Return context tags (principle 5): daypart, season, holiday."""
    when = _local(ts, params)
    tags = {'sleepy'} if when.hour < _NOON else {'bodry'}
    tags.add(('winter', 'spring', 'summer', 'autumn')[(when.month % 12) // 3])
    if when.month == _DECEMBER:  # December reads as the holiday run
        tags.add('newyear')
    return frozenset(tags)


def _mood_bias(reaction: Emoji, mood: float) -> float:
    """Tilt to lively reactions when mood is high, sleepy when low."""
    if 'bodry' in reaction.tags:
        return math.exp(mood)
    if 'sleepy' in reaction.tags:
        return math.exp(-mood)
    return 1.0


class ReactionBrain:
    """The stateful engine: track posts, time them, choose a reaction.

    ``rng`` is injected so tests are deterministic; production uses a
    seeded-at-start ``random.Random``. Tests that need a fixed clock assign
    ``brain.clock`` (a ``() -> float``) after construction.
    """

    clock: Callable[[], float]

    def __init__(
        self,
        params: ReactionParams,
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

    @property
    def posts(self) -> list[tuple[int, int]]:
        """The watched comment targets (last ``watch_posts`` posts)."""
        return self.state.posts

    def note_post(self, chat: int, msg_id: int) -> None:
        """Remember a post (persisted), drop keys for posts that rolled off.

        Only the last ``watch_posts`` posts are ever matched, so once a post
        falls out of the window its (post, person) keys can never fire again --
        pruning them keeps the persisted ``reacted`` set bounded (principle 9).
        Re-noting a known post just moves it to the freshest slot (idempotent),
        so the startup backfill never doubles an entry.
        """
        pair = (chat, msg_id)
        if pair in self.state.posts:
            self.state.posts.remove(pair)
        self.state.posts.append(pair)
        del self.state.posts[: -self.params.watch_posts]  # keep the last N
        self.store.keep_marks(tuple(f'{c}:{m}:' for c, m in self.state.posts))
        self.store.watch(self.state.posts, self.clock())

    def answered(self) -> int:
        """How many (post, commenter) pairs have already been reacted to."""
        return self.store.count_marks()

    def is_comment(self, chat: int, reply_to: int | None) -> bool:
        """Whether a reply in ``chat`` targets one of the tracked posts."""
        return reply_to is not None and (chat, reply_to) in self.state.posts

    def add_pending(self, reaction: Reaction) -> None:
        """Record a reaction scheduled but not yet sent (survives a restart).

        The chosen ``emojis`` ride along, so a restart, /requeue or /status
        shows and then places exactly the reaction that was picked when the
        comment
        was scheduled -- not a fresh random one at send time.
        """
        self.state.pending.append(reaction)
        self._flush_queue()

    def done_pending(self, chat: int, reply_to: int) -> None:
        """Forget a reaction once it has been sent (or abandoned)."""
        self.state.pending = [
            p
            for p in self.state.pending
            if not (p.chat == chat and p.reply_to == reply_to)
        ]
        self._flush_queue()

    def rearm(self, *, renew_all: bool = False) -> list[Reaction]:
        """Return pending reactions to re-arm, renewing missed ones.

        A reaction whose time passed while the host was down is given a fresh
        near-future slot (snapped into the uptime window and spread by the
        spacing cursor), so a night's worth does not fire at once on boot. With
        ``renew_all`` (the /requeue command) every pending reaction is
        recomputed --
        used to flush a queue scheduled under stale timing.
        """
        now = self.clock()
        if renew_all:
            # Re-spread the whole queue from NOW: the heavy-tailed spacing
            # cursor may have run far into the future (a burst under the slow
            # production spacing), which would otherwise keep every reaction
            # days
            # out. /requeue is the operator's reset.
            self.state.next_session_at = now
            self.state.session_start_at = 0.0
            self.state.session_last_at = 0.0
        fresh: list[Reaction] = []
        for queued in self.state.pending:
            due = renew_all or queued.when <= now
            when = self._fire_time(now, engaged=False) if due else queued.when
            fresh.append(replace(queued, when=when))
        self.state.pending = fresh
        self._flush_queue()
        self._save()  # /requeue also reset the session cursors above
        return list(fresh)

    def due_now(self) -> list[Reaction]:
        """Set every pending reaction to fire now and return them."""
        now = self.clock()
        self.state.pending = [
            replace(queued, when=now) for queued in self.state.pending
        ]
        self._flush_queue()
        return list(self.state.pending)

    def schedule(self, key: str, *, engaged: bool) -> float | None:
        """Decide if/when to react to ``key``; None means skip it.

        ``key`` is an opaque dedup handle (the caller ties it to a specific
        post + commenter, so it is once per (post, person)). Marks ``key`` as
        reacted on success. Returns the unix ts at which ``emit`` should run.
        """
        now = self.clock()
        if not self.params.enabled or self.store.marked(key):
            return None
        # like_all likes every comment: place it at the next awake moment
        # (always lands). Otherwise the human-like gates may drop it.
        when = (
            self._fire_time(now, engaged=engaged)
            if self.params.like_all
            else self._planned_when(now, engaged=engaged)
        )
        if when is None:
            return None
        self.store.mark(key)
        self._save()
        return when

    def _planned_when(self, now: float, *, engaged: bool) -> float | None:
        """Return the human-like send time, or None when a gate drops it.

        The gates (principle 7): a random skip, an asleep/too-stale moment, or
        a whole silent day. ``schedule`` bypasses all three when ``like_all``.
        """
        if self.rng.random() < self.params.skip_prob:
            return None
        when = self._plan(now, engaged=engaged)
        if when is None or _is_silent_day(when, self.params):
            return None
        return when

    def _plan(self, now: float, *, engaged: bool) -> float | None:
        """Session-aware send time: bursts inside a session, long gaps between.

        A human does not answer each comment on its own heavy-tailed clock --
        they open the comments now and then, clear whatever piled up in a quick
        BURST (short intra-session gaps), then close the app for a long,
        heavy-tailed while. So ``spacing_*`` is the gap BETWEEN sessions (the
        silence) and ``session_gap_*`` is the gap between reactions INSIDE one,
        so
        two comments written close together land in the SAME burst, seconds
        apart, not smeared an hour apart by a global cursor.

        Returns the send ts, or None when the comment cannot be answered at an
        awake, host-up moment within ``max_reply_delay_sec`` -- a human does
        not reply to something they only saw many hours late.
        """
        latency = _lognormal(
            self.rng, self.params.latency_log_mu, self.params.latency_log_sigma
        )
        if engaged:  # principle 8: an engaged commenter gets a faster reaction
            latency *= self.params.feedback_speedup
        earliest = now + latency

        # Still inside the current session? Then this comment joins the burst.
        # The test is on PLACED time (session_last_at may sit in the future
        # when the session itself is scheduled ahead), so a whole gap's backlog
        # funnels into one upcoming burst, not each opening its own session.
        s_last = self.state.session_last_at
        s_start = self.state.session_start_at
        in_session = (
            s_last > 0.0
            and earliest <= s_last + self.params.session_idle_sec
            and (s_last - s_start) < self.params.session_max_sec
            and (s_last - now) < _SESSION_REACH_SEC  # not a far-ahead session
        )
        if in_session:
            gap = _lognormal(
                self.rng,
                self.params.session_gap_log_mu,
                self.params.session_gap_log_sigma,
            )
            when = max(earliest, s_last + gap)
            self.state.session_last_at = when
            return _jitter(when, self.params, self.rng)

        # Otherwise open a NEW session, no earlier than the between-session
        # cursor, placed at a plausibly-awake moment (not the window's edge).
        start = max(earliest, self.state.next_session_at)
        placed = self._place(start)
        if placed is None or placed - now > self.params.max_reply_delay_sec:
            return None  # nothing awake in reach -> the comment goes stale
        spacing = _lognormal(
            self.rng, self.params.spacing_log_mu, self.params.spacing_log_sigma
        )
        self.state.session_start_at = placed
        self.state.session_last_at = placed
        self.state.next_session_at = placed + spacing
        return _jitter(placed, self.params, self.rng)

    def _fire_time(self, now: float, *, engaged: bool) -> float:
        """Re-slot a committed pending reaction (rearm) into the next session.

        Unlike ``schedule`` a pending reaction is already committed, so it must
        land
        somewhere: if the session planner would drop it (asleep / stale), fall
        back to the next awake moment rather than returning nothing.
        """
        when = self._plan(now, engaged=engaged)
        if when is not None:
            return when
        placed = self._place(max(now, self.state.next_session_at))
        return _jitter(
            placed if placed is not None else now, self.params, self.rng
        )

    def mark_alive(self, now: float) -> None:
        """Heartbeat: record that the host is up at this hour (decayed).

        Called on a timer while running, this builds the real on-hours curve.
        A long gap (a shutdown) just decays the old buckets -- the dead hours
        are never credited, so the learned uptime tracks reality.

        ONE ROW, and nothing else. This runs every sixty seconds, and while
        the curve lived in the JSON blob each beat rewrote the whole block --
        about 15 MB a day to store a number that fits in eight bytes. It is
        the write this whole tier was measured against, so the test beside it
        asserts the cost rather than trusting the shape.
        """
        self.store.note_hour(
            _local(now, self.params).hour,
            self.params.uptime_half_life_sec,
            now,
        )

    def learned_hours(self) -> dict[int, float]:
        """Return the learned uptime curve, decayed to now (/status)."""
        return self.store.hours(self.params.uptime_half_life_sec, self.clock())

    def _alive_fraction(self, ts: float) -> float:
        """How up the host tends to be at ``ts``'s hour, in [0, 1].

        Blends the LEARNED uptime with the declared-window prior by confidence:
        cold, it follows the window; with history, it follows what the NAS
        actually does -- even hours outside the declared window.
        """
        alive = self.store.hours(self.params.uptime_half_life_sec, ts)
        peak = max(alive.values(), default=0.0)
        hour = _local(ts, self.params).hour
        observed = alive.get(hour, 0.0) / peak if peak else 0.0
        prior = 1.0 if _in_window(float(hour), self.params) else 0.0
        target = self.params.uptime_learn_obs
        total = sum(alive.values())
        conf = min(1.0, total / target) if target > 0 else 1.0
        return conf * observed + (1.0 - conf) * prior

    def _effective_weight(self, ts: float) -> float:
        """Density x how likely the host is up -- the schedulable weight."""
        return _density_weight(ts, self.params) * self._alive_fraction(ts)

    def _place(self, ts: float) -> float | None:
        """Return a plausibly-awake, host-up send moment at or after ``ts``.

        If ``ts`` already sits at a live moment it is usually kept (a daytime
        comment is answered promptly). Otherwise we SAMPLE a moment across the
        reachable awake time in proportion to the activity density -- so an
        overnight backlog scatters across the morning by shape instead of
        piling on the window's leading edge at HH:00 (the old
        hop-to-first-live-hour fingerprint). Returns None when no awake moment
        is reachable within ``max_reply_delay_sec`` (the comment goes stale).
        """
        peak = max(
            (self._effective_weight(ts + h * 3600.0) for h in range(24)),
            default=0.0,
        )
        if peak <= 0.0:
            return None
        here = self._effective_weight(ts)
        if here > 0.0 and self.rng.random() < here / peak:
            return ts  # already awake -> answer promptly
        return self._sample_awake(ts)

    def _sample_awake(self, ts: float) -> float | None:
        """Density-weighted pick of an awake 5-min slot within the horizon."""
        deadline = ts + self.params.max_reply_delay_sec
        slots: list[float] = []
        weights: list[float] = []
        t = ts
        while t <= deadline:
            weight = self._effective_weight(t)
            if weight > 0.0:
                slots.append(t)
                weights.append(weight)
            t += _PLACE_STEP_SEC
        if not slots:
            return None
        return weighted_choice(self.rng, slots, weights)

    def pick_like(self, key: str) -> list[Emoji]:
        """One like for a target, WEIGHTED and DETERMINISTIC in ``key``.

        Drawn from the like pool through the same human machinery as the
        reactions
        (principles 3,4,5): base favourites, recency suppression, mood and
        context tags. Still seeded by ``key`` (the target message id) so the
        pick is reproducible per target given the persisted state -- but now
        the pick is recorded into ``reaction_last``, so consecutive reactions
        in a
        burst avoid repeating the same emoji, which the old uniform ``choice``
        did not. Always one like; an empty pool yields [].
        """
        return self._choose(self.params.like_pool, key)

    def pick_reaction(self, key: str) -> list[Emoji]:
        """Pick the thread sticker, WEIGHTED and DETERMINISTIC in ``key``.

        Same as ``pick_like`` but drawn from the reaction ``pool``, so a
        sticker
        honours base favourites, recency, mood and season/daypart tags (a
        sleepy reaction in the morning, a lively one when mood runs high)
        instead of
        a flat uniform pick. Seeded by the target id; records recency. An empty
        pool yields [].
        """
        return self._choose(self.params.pool, key)

    def _choose(self, pool: tuple[Emoji, ...], key: str) -> list[Emoji]:
        """Weighted, reproducible-in-key draw of one emoji; records recency.

        Shared by ``pick_like`` and ``pick_reaction``. Weights come from
        ``_weight`` (base * recency * mood * context), so every emoji people
        actually see carries the full human machinery. Seeded by ``key`` for
        per-target reproducibility, then the pick is written to
        ``reaction_last`` so the next draw suppresses it -- no back-to-back
        repeats across a burst of reactions.
        """
        if not pool:
            return []
        now = self.clock()
        self._step_mood(now)  # principle 4: advance the daily mood first
        weights = [self._weight(reaction, now) for reaction in pool]
        roll = random.Random(key)  # noqa: S311 -- mimicry, reproducible-in-key
        chosen = weighted_choice(roll, pool, weights)
        self.state.reaction_last[chosen.id] = now  # principle 3: recency
        self.store.note_emoji(chosen.id, now)
        self._save()
        return [chosen]

    def _control(self) -> relationship.Control:
        """Build the shared Berlyne control from this engine's params."""
        p = self.params
        return relationship.Control(
            wundt=attachment.WundtParams(
                c1=p.exposure_c1, c2=p.exposure_c2, k=p.exposure_k
            ),
            take_gain=p.like_control_gain,
            recip_target=p.recip_fraction_target,
            recip_gain=p.recip_control_gain,
            take_cap=p.like_max_per_day,
            recip_cap=p.sticker_max_per_day,
            arc=p.arc,
            # One answering session IS the burst this engine clumps into, so
            # the clumping factor reuses that gate rather than adding a knob.
            burst_gap_sec=p.session_idle_sec,
        )

    def decide_engage(self, person: int, comment: int = 0) -> bool:
        """Whether to like ``person``'s comment, steering p -> the Wundt peak.

        Exposure control: the running ``taken/offered`` is nudged toward the
        peak (~0.67) by a per-comment Bernoulli, so a heavy commenter is
        throttled (no desperate like-everything) while a newcomer is kept warm.
        The FIRST comment from a person is always engaged (a warm hello); the
        control starts from the second. Counts the comment either way, so a
        rescan never re-rolls a decided comment.

        Decides BEFORE it records, and then records once, naming the outcome:
        a comment we passed on is logged ``ignore``, one we answered ``like``.
        Recording the chance first and the answer after would leave the log
        saying we ignored every comment we ever liked.

        ``comment`` is that comment's message id, and it is what the history
        row is ABOUT: without it the log could say we liked four of their
        comments but not which four.
        """
        led, control, now = self.ledger, self._control(), self.clock()
        first = led.row(person).offered == 0
        take = first or self.rng.random() < led.take_prob(person, control, now)
        # The cap is the last word: a take it refuses is an ignore.
        ok = take and led.spend_take(control, now, self._tz())
        if ok:
            led.add_take(person, (comment,), control, now)
        else:
            led.add_offer(person, (comment,), now)
        self._save()
        return ok

    def decide_sticker(
        self, person: int, comment: int = 0, *, content_ok: bool
    ) -> bool:
        """Whether to upgrade this engagement to a sticker, steering r -> 0.20.

        Reciprocity control among the comments we engage: the stronger,
        message-shaped sticker replaces the plain like about one time in five
        (``recip_fraction_target``), nudged by feedback so recip/taken
        converges. ``content_ok`` is False for a question/link/business comment
        (a sticker reads as a non-sequitur there) -- then it stays a like and
        the reciprocity roll is not consumed. Capped per day (stickers are the
        message ban surface). Call once, only when ``decide_engage`` returned
        True (so the take already counts this comment).
        """
        if not content_ok:
            return False
        led = self.ledger
        ctrl = self._control()
        now = self.clock()
        prob = led.recip_prob(person, ctrl, now, taken_now=True)
        if self.rng.random() >= prob:
            return False
        if not led.spend_recip(ctrl, now, self._tz()):
            return False
        led.bump_recip(person, comment, now)
        self._save()
        return True

    def warmth(self) -> list[relationship.Warmth]:
        """Per-commenter attachment readout for /status, most recent first."""
        return relationship.warmth(self.ledger, self._control())

    def likes_today(self, now: float) -> int:
        """Engagements (likes) placed on the local date of ``now`` (else 0)."""
        return self.ledger.takes_today(now, self._tz())

    def stickers_today(self, now: float) -> int:
        """Stickers placed on the local date of ``now`` (else 0)."""
        return self.ledger.recips_today(now, self._tz())

    def _tz(self) -> float:
        """Return the persona timezone offset (for the daily counters)."""
        return self.params.tz_offset_hours

    def _weight(self, reaction: Emoji, now: float) -> float:
        """Return the selection weight: base*recency*mood*context (3,4,5)."""
        dt = now - self.state.reaction_last.get(reaction.id, 0.0)
        weight = reaction.base * recency_penalty(
            dt, self.params.recency_half_life_sec
        )
        weight *= _mood_bias(reaction, self.state.mood)
        if _context_tags(now, self.params) & set(reaction.tags):
            weight *= 2.0  # a context match is the strongest human signal
        return max(weight, 0.0)

    def _step_mood(self, now: float) -> None:
        """Advance the AR(1) mood once per calendar day (principle 4)."""
        day = _local(now, self.params).strftime('%Y-%m-%d')
        if day == self.state.mood_day:
            return
        noise = self.rng.gauss(0.0, self.params.mood_sigma)
        self.state.mood = self.params.mood_phi * self.state.mood + noise
        self.state.mood_day = day

    def _load(self) -> ReactionState:
        """Reload the state block, or start fresh when there is none."""
        raw = self.store.read()
        return ReactionState(
            mood=codec.num(raw.get('mood')),
            mood_day=codec.text(raw.get('mood_day')),
            next_session_at=codec.num(raw.get('next_at')),
            session_start_at=codec.num(raw.get('start_at')),
            session_last_at=codec.num(raw.get('last_at')),
            reaction_last=self.store.emoji_seen(),
            posts=self.store.watched(),
            pending=[_queued(row) for row in self.store.queued()],
        )

    def _flush_queue(self) -> None:
        """Write the pending queue to its own table.

        Still the whole collection, and that is what the caller means: the
        queue is re-planned as a set (a /requeue re-arms every timer). What
        changed is that writing it no longer drags the mood, the counters and
        the learned uptime curve along with it.
        """
        self.store.enqueue(_queue_row(q) for q in self.state.pending)

    def _save(self) -> None:
        """Publish this engine's state block -- SCALARS ONLY.

        The session marks used to be a nested ``session`` object, which is a
        dict, which is a collection, which is what the rule forbids. Three
        flat keys instead: it is three values, and nesting them said nothing
        the names did not already say.
        """
        self.store.write(
            {
                'mood': self.state.mood,
                'mood_day': self.state.mood_day,
                'next_at': self.state.next_session_at,
                'start_at': self.state.session_start_at,
                'last_at': self.state.session_last_at,
            },
        )


def _peaks(
    value: object,
) -> tuple[tuple[float, float, float], ...]:
    """Parse a JSON list of [mean, sigma, weight] peaks."""
    out = []
    for row in codec.rows(value):
        mean, sigma, weight = codec.rows(row)
        out.append((codec.num(mean), codec.num(sigma), codec.num(weight)))
    return tuple(out)


def _pool(data: dict[str, object], kind: str) -> tuple[Emoji, ...]:
    """Build one emoji pool from the unified top-level ``emoji`` array."""
    return tuple(config.emoji_of(config.emoji_catalog(data), kind))


def load_reaction_params(data: dict[str, object]) -> ReactionParams:
    """Load the reaction engine's params from the 'reactions' JSON key.

    Every scalar knob reads its own key and falls back to its own declared
    default (``core/codec.py``); only what a plain key cannot express is
    spelled out here -- the two emoji pools, the Gaussian day curves, and
    the uptime window whose JSON keys carry an ``_hour`` suffix.

    ``quiet_hours`` is NOT among them any more. It used to be read here with
    an ``or`` that turned a written ``[]`` back into the 2-6 default, so the
    one thing an operator could write to mean "never silent" meant its
    opposite. ``frozenset[int]`` is a reader ``codec`` already has, and the
    key is now gone from the constants file, so an absent key still gives the
    declared default and a written one finally means what it says.
    """
    cfg = codec.engine(data, 'reactions')
    return codec.decode(
        ReactionParams,
        cfg,
        {
            'pool': _pool(data, 'reaction'),
            'like_pool': _pool(data, 'like'),
            'hours_weekday': _peaks(cfg.get('hours_weekday'))
            or ReactionParams.hours_weekday,
            'hours_weekend': _peaks(cfg.get('hours_weekend'))
            or ReactionParams.hours_weekend,
            'active_start': codec.num(
                cfg.get('active_start_hour'), ReactionParams.active_start
            ),
            'active_end': codec.num(
                cfg.get('active_end_hour'), ReactionParams.active_end
            ),
            'arc': relationship.load_arc(cfg),
        },
    )
