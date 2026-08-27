# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Human-like cat-emoji reactions to people who comment on the last posts.

The aggregator posts announces; this module lets its user account react to a
commenter's comment with a premium cat emoji ONCE, timed and chosen so the
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
   sessions; ``session_gap_*`` is the gap between cats INSIDE one -- so bursts
   then silence, and co-occurring comments land in the same burst.
3. Selection has memory: weight = base preference * recency penalty (an
   exponential recovery from the last time that cat was used), so favourites
   lead and just-used cats fade.
4. A latent "mood" does an AR(1) random walk day to day and tilts selection
   toward sleepy vs. lively cats -- day-to-day coherence, not memorylessness.
5. Context tags (daypart, season, holiday) re-weight the pool: a sleepy cat in
   the morning, a festive one in December.
6. Jitter defeats the ":00 scheduler fingerprint": the fire time gets a random
   sub-minutes offset.
7. Built-in imperfection: a comment is sometimes ignored, a day is sometimes
   silent, and once in a while a second cat follows the first.
8. Feedback reactivity: a commenter who is themselves replying to us gets a
   faster reaction.
9. State is persisted (mood, last-send cursor, per-cat recency, who was already
   catted), because principles 2-4 need memory across restarts.

All texts/ids live in the constants JSON, so this source stays ASCII.
"""

from __future__ import annotations

import json
import math
import random
import time
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from minions.userbot.core import attachment
from minions.userbot.core import humanize
from minions.userbot.core import relationship
from minions.userbot.core.humanize import recency_penalty
from minions.userbot.core.humanize import weighted_choice

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path

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
# A catted dedup key is 'chat:root:person[:msg]', so the person is field 3.
_CATTED_PERSON_FIELDS = 3
# datetime.weekday(): Monday is 0, so Saturday (5) and up is the weekend.
_SATURDAY = 5
# Local hours: midnight..noon reads sleepy, noon..midnight bodry.
_NOON = 12
# Calendar month numbers used as context switches.
_DECEMBER = 12


@dataclass(frozen=True)
class CatEmoji:
    """One premium cat emoji in the pool, with its selection knobs."""

    emoji_id: str
    fallback: str
    base: float  # a priori preference (favourites > 1)
    tags: tuple[str, ...]  # e.g. ('sleepy',), ('bodry', 'newyear')


@dataclass(frozen=True)
class Cat:
    """A scheduled cat reaction: react to ``reply_to`` in ``chat``.

    ``reply_to`` is the commenter's message id -- the cat emoji reaction is
    placed directly on it (not a reply in the thread). ``root`` is the post
    (thread root) the comment sits under; it is kept for the
    once-per-(post, person) dedup key and the /status readout, not for
    placement (a reaction needs no threading). For a plain group it equals
    ``reply_to``.

    ``emojis`` is the exact cat(s) chosen for THIS reaction, decided when the
    comment is scheduled (not at send time), so the queue is deterministic and
    inspectable: /status and /requeue can show which cat lands where. Each item
    is an ``(emoji_id, fallback)`` pair; usually one, occasionally two (the
    rare double).
    """

    chat: int
    reply_to: int  # the commenter's message id
    root: int  # the thread root (post) for discussion threading
    when: float
    text: str = ''  # a snippet of the comment being answered (for status)
    emojis: tuple[tuple[str, str], ...] = ()  # the chosen (id, fallback) cats
    kind: str = 'react'  # 'react' = a like reaction; 'reply' = thread sticker


def _emojis_from_entry(raw: object) -> tuple[tuple[str, str], ...]:
    """Parse a persisted ``emojis`` list of [id, fallback] pairs."""
    rows = raw if isinstance(raw, list) else []
    return tuple(
        (str(row[0]), str(row[1]))
        for row in rows
        if isinstance(row, (list, tuple)) and len(row) == _EMOJI_ROW_LEN
    )


def _cat_from_entry(entry: dict[str, object], when: float) -> Cat:
    """Rebuild a Cat from a persisted dict (root defaults to reply_to)."""
    reply_to = int(entry['reply_to'])
    return Cat(
        chat=int(entry['chat']),
        reply_to=reply_to,
        root=int(entry.get('root', reply_to)),
        when=when,
        text=str(entry.get('text', '')),
        emojis=_emojis_from_entry(entry.get('emojis')),
        kind=str(entry.get('kind', 'react')),
    )


@dataclass(frozen=True)
class CatParams:
    """Every tunable, loaded from the constants JSON 'cats' section."""

    enabled: bool
    # Like ABSOLUTELY every comment: bypass the human-like gates (skip_prob,
    # silent days, the asleep/stale drop) so each comment always gets a like
    # reaction. Dedup and the manual-reply check still apply -- a comment is
    # liked once, not repeatedly. Off = the human-like behaviour.
    like_all: bool
    # When the target is a CHANNEL, its comments live in the linked discussion
    # group. True: resolve each post's discussion thread and react only there
    # (so cats land in the channel post's comments). False: match replies to
    # the post id directly (the target is a plain group).
    comments_in_discussion: bool
    # Whether to also drop a cat reaction on our OWN fresh posts (immediately,
    # no human-like wait). Optional and off by default: the engine's job is
    # reacting to COMMENTERS; liking our own posts is a separate extra.
    react_to_posts: bool
    watch_posts: int
    hours_weekday: tuple[tuple[float, float, float], ...]
    hours_weekend: tuple[tuple[float, float, float], ...]
    quiet_hours: frozenset[int]
    # The DECLARED uptime window in local hours [start, end) -- a prior only.
    # The engine also LEARNS the real on-hours (mark_alive), and blends the two
    # by confidence, so it adapts to whatever hours the NAS is actually up, not
    # just this rule of thumb. start >= end (or 0/24) means "up all day".
    active_start: float
    active_end: float
    # How the learned uptime is weighed: uptime_half_life_sec fades old
    # observations (so a changed schedule is followed), uptime_learn_obs is how
    # many heartbeats of history earn full trust in the learned curve over the
    # declared window. skip_if_manually_replied drops the cat when the operator
    # has already replied to that comment by hand.
    uptime_half_life_sec: float
    uptime_learn_obs: float
    skip_if_manually_replied: bool
    # The persona's UTC offset: hours/dates are read in THIS timezone, so the
    # cadence matches the legend, not the server's clock (principle 9).
    tz_offset_hours: float
    latency_log_mu: float
    latency_log_sigma: float
    spacing_log_mu: float
    spacing_log_sigma: float
    jitter_sec: float
    skip_prob: float
    double_prob: float
    double_gap_sec: float
    silent_day_prob: float
    recency_half_life_sec: float
    mood_phi: float
    mood_sigma: float
    feedback_speedup: float
    # Session model (see CatBrain._plan): spacing_* is the long gap BETWEEN
    # comment-answering sessions; the fields below shape ONE session.
    session_gap_log_mu: float  # log-mean of the short intra-session gap
    session_gap_log_sigma: float
    session_idle_sec: float  # a silence longer than this ends the session
    session_max_sec: float  # hard cap on one session's span
    max_reply_delay_sec: float  # a cat older than this is too stale -> skip
    pool: tuple[CatEmoji, ...]
    # The LIKE pool: the emoji placed as the default reaction. Chosen
    # pseudo-randomly but DETERMINISTICALLY -- seeded by the target id, so the
    # same comment/post always yields the same like: varied across targets, yet
    # recomputable after a restart (no persisted cursor). Separate from the cat
    # ``pool``.
    like_pool: tuple[CatEmoji, ...]
    # How often (seconds) the bot re-scans the targets on its own -- picking up
    # posts created (and commented on) while it runs, without waiting for a
    # restart or a manual /requeue. 0 turns the auto-rescan off.
    rescan_sec: float
    # --- Berlyne attachment control (per commenter) -----------------------
    # When on, we do NOT like every comment. Instead the fraction of a
    # person's comments we engage (like or sticker) is steered toward the
    # Wundt peak (~0.67): liking everything reads as desperate, so a heavy
    # commenter is throttled while a newcomer is still acknowledged. Off
    # reproduces the old behaviour (like_all likes every comment). The like
    # reaction is the EXPOSURE act; the thread sticker is the RECIPROCITY act.
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


@dataclass
class CatState:
    """The persisted memory that principles 2-4 and once-per-person need.

    ``posts`` (the watched comment targets) and ``pending`` (cats scheduled but
    not yet sent) are persisted too, so a nightly NAS shutdown does not lose
    which posts to watch or the cats due after it comes back.
    """

    mood: float = 0.0
    mood_day: str = ''  # ISO date of the last mood step (drift once a day)
    last_send: float = 0.0  # unix ts of the most recent cat sent
    next_session_at: float = (
        0.0  # earliest the NEXT session may open (spacing)
    )
    session_start_at: float = 0.0  # when the current burst began
    session_last_at: float = 0.0  # last cat placed in the current burst
    cat_last: dict[str, float] = field(default_factory=dict)  # id -> last ts
    catted: set[str] = field(default_factory=set)  # (post, person) keys done
    posts: list[tuple[int, int]] = field(default_factory=list)  # comment tgts
    pending: list[dict[str, object]] = field(default_factory=list)  # due cats
    alive: dict[str, float] = field(
        default_factory=dict
    )  # hour -> decayed obs
    alive_ts: float = 0.0  # last heartbeat, for decay
    # Per-commenter relationship memory (the shared Berlyne ledger), spanning
    # posts so a person is remembered across our posts (the point of adapting
    # to each individual): offered=commented, taken=engaged (liked),
    # recip=stickered, with the date-keyed like/sticker daily caps.
    ledger: relationship.Ledger = field(
        default_factory=relationship.Ledger
    )


def _local(ts: float, params: CatParams) -> datetime:
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


def _in_window(hour: float, params: CatParams) -> bool:
    """Whether ``hour`` is inside the host's uptime window [start, end)."""
    if params.active_start >= params.active_end:
        return True  # no window configured -> always up
    return params.active_start <= hour < params.active_end


def _density_weight(ts: float, params: CatParams) -> float:
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


def _jitter(ts: float, params: CatParams, rng: random.Random) -> float:
    """Add a random sub-minutes offset so timestamps are not on the :00.

    Principle 6: a scheduled task firing on the exact minute is a fingerprint.
    """
    return ts + rng.uniform(0.0, params.jitter_sec)


def _is_silent_day(ts: float, params: CatParams) -> bool:
    """Whether the whole day at ``ts`` is a silent one (principle 7).

    Deterministic per date (seeded by the date) so a restart does not flip a
    day that was already decided.
    """
    return humanize.is_silent_day(
        ts, params.tz_offset_hours, params.silent_day_prob
    )


def _context_tags(ts: float, params: CatParams) -> frozenset[str]:
    """Return context tags (principle 5): daypart, season, holiday."""
    when = _local(ts, params)
    tags = {'sleepy'} if when.hour < _NOON else {'bodry'}
    tags.add(('winter', 'spring', 'summer', 'autumn')[(when.month % 12) // 3])
    if when.month == _DECEMBER:  # December reads as the holiday run
        tags.add('newyear')
    return frozenset(tags)


def _mood_bias(cat: CatEmoji, mood: float) -> float:
    """Tilt to lively cats when mood is high, sleepy when low (principle 4)."""
    if 'bodry' in cat.tags:
        return math.exp(mood)
    if 'sleepy' in cat.tags:
        return math.exp(-mood)
    return 1.0


class CatBrain:
    """The stateful engine: track posts, decide when, and choose which cat.

    ``rng`` is injected so tests are deterministic; production uses a
    seeded-at-start ``random.Random``. Tests that need a fixed clock assign
    ``brain.clock`` (a ``() -> float``) after construction.
    """

    clock: Callable[[], float]

    def __init__(
        self,
        params: CatParams,
        path: Path,
        rng: random.Random | None = None,
    ) -> None:
        """Bind the params and state path; seed the RNG."""
        self.params = params
        self.path = path
        self.rng = rng or random.Random()  # noqa: S311 -- mimicry, not crypto
        self.clock = time.time
        self.state = self._load()

    @property
    def posts(self) -> list[tuple[int, int]]:
        """The watched comment targets (last ``watch_posts`` posts)."""
        return self.state.posts

    def note_post(self, chat: int, msg_id: int) -> None:
        """Remember a post (persisted), drop keys for posts that rolled off.

        Only the last ``watch_posts`` posts are ever matched, so once a post
        falls out of the window its (post, person) keys can never fire again --
        pruning them keeps the persisted ``catted`` set bounded (principle 9).
        Re-noting a known post just moves it to the freshest slot (idempotent),
        so the startup backfill never doubles an entry.
        """
        pair = (chat, msg_id)
        if pair in self.state.posts:
            self.state.posts.remove(pair)
        self.state.posts.append(pair)
        del self.state.posts[: -self.params.watch_posts]  # keep the last N
        live = tuple(f'{c}:{m}:' for c, m in self.state.posts)
        self.state.catted = {
            k for k in self.state.catted if k.startswith(live)
        }
        self._save()

    def is_comment(self, chat: int, reply_to: int | None) -> bool:
        """Whether a reply in ``chat`` targets one of the tracked posts."""
        return reply_to is not None and (chat, reply_to) in self.state.posts

    def add_pending(self, cat: Cat) -> None:
        """Record a cat scheduled but not yet sent (survives a restart).

        The chosen ``emojis`` ride along, so a restart, /requeue or /status
        shows and then places exactly the cat that was picked when the comment
        was scheduled -- not a fresh random one at send time.
        """
        self.state.pending.append(
            {
                'chat': cat.chat,
                'reply_to': cat.reply_to,
                'root': cat.root,
                'when': cat.when,
                'text': cat.text,
                'emojis': [[eid, fb] for eid, fb in cat.emojis],
                'kind': cat.kind,
            }
        )
        self._save()

    def done_pending(self, chat: int, reply_to: int) -> None:
        """Forget a cat once it has been sent (or abandoned)."""
        self.state.pending = [
            p
            for p in self.state.pending
            if not (p['chat'] == chat and p['reply_to'] == reply_to)
        ]
        self._save()

    def rearm(self, *, renew_all: bool = False) -> list[Cat]:
        """Return the pending cats to re-arm, renewing missed ones (or all).

        A cat whose time passed while the host was down is given a fresh
        near-future slot (snapped into the uptime window and spread by the
        spacing cursor), so a night's worth does not fire at once on boot. With
        ``renew_all`` (the /requeue command) every pending cat is recomputed --
        used to flush a queue scheduled under stale timing.
        """
        now = self.clock()
        if renew_all:
            # Re-spread the whole queue from NOW: the heavy-tailed spacing
            # cursor may have run far into the future (a burst under the slow
            # production spacing), which would otherwise keep every cat days
            # out. /requeue is the operator's reset.
            self.state.next_session_at = now
            self.state.session_start_at = 0.0
            self.state.session_last_at = 0.0
        out: list[Cat] = []
        for entry in self.state.pending:
            when = float(entry['when'])
            if renew_all or when <= now:
                when = self._fire_time(now, engaged=False)
                entry['when'] = when
            out.append(_cat_from_entry(entry, when))
        self._save()
        return out

    def due_now(self) -> list[Cat]:
        """Set EVERY pending cat's time to now and return them (answer all)."""
        now = self.clock()
        out = [_cat_from_entry(entry, now) for entry in self.state.pending]
        for entry in self.state.pending:
            entry['when'] = now
        self._save()
        return out

    def schedule(self, key: str, *, engaged: bool) -> float | None:
        """Decide if/when to cat ``key``; None means no cat this time.

        ``key`` is an opaque dedup handle (the caller ties it to a specific
        post + commenter, so it is once per (post, person)). Marks ``key`` as
        catted on success. Returns the unix ts at which ``emit`` should run.
        """
        now = self.clock()
        if not self.params.enabled or key in self.state.catted:
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
        self.state.catted.add(key)
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
        silence) and ``session_gap_*`` is the gap between cats INSIDE one, so
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
        """Re-slot a committed pending cat (rearm) into the next session.

        Unlike ``schedule`` a pending cat is already committed, so it must land
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
        """
        prev = self.state.alive_ts
        if prev > 0 and self.params.uptime_half_life_sec > 0:
            decay = 0.5 ** ((now - prev) / self.params.uptime_half_life_sec)
            self.state.alive = {
                h: w * decay for h, w in self.state.alive.items()
            }
        hour = str(_local(now, self.params).hour)
        self.state.alive[hour] = self.state.alive.get(hour, 0.0) + _ALIVE_STEP
        self.state.alive_ts = now
        self._save()

    def _alive_fraction(self, ts: float) -> float:
        """How up the host tends to be at ``ts``'s hour, in [0, 1].

        Blends the LEARNED uptime with the declared-window prior by confidence:
        cold, it follows the window; with history, it follows what the NAS
        actually does -- even hours outside the declared window.
        """
        peak = max(self.state.alive.values(), default=0.0)
        hour = _local(ts, self.params).hour
        observed = self.state.alive.get(str(hour), 0.0) / peak if peak else 0.0
        prior = 1.0 if _in_window(float(hour), self.params) else 0.0
        target = self.params.uptime_learn_obs
        total = sum(self.state.alive.values())
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

    def pick_like(self, key: str) -> list[CatEmoji]:
        """One like for a target, WEIGHTED and DETERMINISTIC in ``key``.

        Drawn from the like pool through the same human machinery as the cats
        (principles 3,4,5): base favourites, recency suppression, mood and
        context tags. Still seeded by ``key`` (the target message id) so the
        pick is reproducible per target given the persisted state -- but now
        the pick is recorded into ``cat_last``, so consecutive reactions in a
        burst avoid repeating the same emoji, which the old uniform ``choice``
        did not. Always one like; an empty pool yields [].
        """
        return self._choose(self.params.like_pool, key)

    def pick_cat(self, key: str) -> list[CatEmoji]:
        """One cat (the thread sticker), WEIGHTED and DETERMINISTIC in ``key``.

        Same as ``pick_like`` but drawn from the cat ``pool``, so a sticker
        honours base favourites, recency, mood and season/daypart tags (a
        sleepy cat in the morning, a lively one when mood runs high) instead of
        a flat uniform pick. Seeded by the target id; records recency. An empty
        pool yields [].
        """
        return self._choose(self.params.pool, key)

    def _choose(self, pool: tuple[CatEmoji, ...], key: str) -> list[CatEmoji]:
        """Weighted, reproducible-in-key draw of one emoji; records recency.

        Shared by ``pick_like`` and ``pick_cat``. Weights come from ``_weight``
        (base * recency * mood * context), so the emoji people actually SEE
        inherits the same human machinery that the (otherwise unused) ``emit``
        path had. Seeded by ``key`` for per-target reproducibility, then the
        pick is written to ``cat_last`` so the next draw suppresses it -- no
        back-to-back repeats across a burst of reactions.
        """
        if not pool:
            return []
        now = self.clock()
        self._step_mood(now)  # principle 4: advance the daily mood first
        weights = [self._weight(cat, now) for cat in pool]
        roll = random.Random(key)  # noqa: S311 -- mimicry, reproducible-in-key
        chosen = weighted_choice(roll, pool, weights)
        self.state.cat_last[chosen.emoji_id] = now  # principle 3: recency
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
        )

    def _grant_engage(self, person: str) -> bool:
        """Commit one engagement (like) to the ledger if the cap allows."""
        led = self.state.ledger
        if not led.spend_take(self._control(), self.clock(), self._tz()):
            return False
        led.bump_take(person)
        return True

    def decide_engage(self, person: str) -> bool:
        """Whether to like ``person``'s comment, steering p -> the Wundt peak.

        Exposure control: the running ``taken/offered`` is nudged toward the
        peak (~0.67) by a per-comment Bernoulli, so a heavy commenter is
        throttled (no desperate like-everything) while a newcomer is kept warm.
        The FIRST comment from a person is always engaged (a warm hello); the
        control starts from the second. Records the comment as offered either
        way, so a rescan never re-rolls a decided comment.
        """
        led = self.state.ledger
        first = led.offered.get(person, 0) == 0
        prob = led.take_prob(person, self._control())
        take = first or self.rng.random() < prob
        led.add_offer(person)  # count this comment (recorded before granting)
        ok = self._grant_engage(person) if take else False
        self._save()
        return ok

    def decide_sticker(self, person: str, *, content_ok: bool) -> bool:
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
        led = self.state.ledger
        ctrl = self._control()
        prob = led.recip_prob(person, ctrl, taken_now=True)
        if self.rng.random() >= prob:
            return False
        if not led.spend_recip(ctrl, self.clock(), self._tz()):
            return False
        led.recip[person] = led.recip.get(person, 0) + 1
        self._save()
        return True

    def warmth(self) -> list[relationship.Warmth]:
        """Per-commenter attachment readout for /status, most recent first."""
        return relationship.warmth(self.state.ledger, self._control())

    def remember(self, person: str, label: str) -> None:
        """Cache a commenter's @name for /status (persisted)."""
        if not label or label == person:
            return
        self.state.ledger.remember(person, label)
        self._save()

    def likes_today(self, now: float) -> int:
        """Engagements (likes) placed on the local date of ``now`` (else 0)."""
        return self.state.ledger.takes_today(now, self._tz())

    def stickers_today(self, now: float) -> int:
        """Stickers placed on the local date of ``now`` (else 0)."""
        return self.state.ledger.recips_today(now, self._tz())

    def _tz(self) -> float:
        """Return the persona timezone offset (for the daily counters)."""
        return self.params.tz_offset_hours

    def emit(self) -> list[CatEmoji]:
        """Pick the cat(s) to send now and record the send (principles 3,4,7).

        Returns one cat, or two when the rare "second cat" fires. An empty pool
        yields an empty list (the caller then sends nothing).
        """
        now = self.clock()
        self._step_mood(now)
        if not self.params.pool:
            return []
        cats = [self._pick(now)]
        if self.rng.random() < self.params.double_prob:  # principle 7
            cats.append(self._pick(now))
        for cat in cats:
            self.state.cat_last[cat.emoji_id] = now
        self.state.last_send = now
        self._save()
        return cats

    def _pick(self, now: float) -> CatEmoji:
        """One weighted cat draw at ``now``."""
        pool = self.params.pool
        weights = [self._weight(c, now) for c in pool]
        return weighted_choice(self.rng, pool, weights)

    def _weight(self, cat: CatEmoji, now: float) -> float:
        """Return the selection weight: base*recency*mood*context (3,4,5)."""
        dt = now - self.state.cat_last.get(cat.emoji_id, 0.0)
        weight = cat.base * recency_penalty(
            dt, self.params.recency_half_life_sec
        )
        weight *= _mood_bias(cat, self.state.mood)
        if _context_tags(now, self.params) & set(cat.tags):
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

    def _backfill_ledger(self, state: CatState) -> None:
        """Seed the relationship ledger from historically-liked comments.

        A state file written before the attachment control has an empty ledger
        but a full ``catted`` set (every comment we already liked). Seed
        offered=taken per commenter from it, so the /status coefficients show
        the history at once instead of only new comments; the control then
        steers each person from there. Runs only while the ledger is empty, so
        a real engagement (which persists the ledger) supersedes it.
        """
        if state.ledger.offered or not state.catted:
            return
        for key in state.catted:
            parts = key.split(':')
            person = parts[2] if len(parts) >= _CATTED_PERSON_FIELDS else ''
            if person:
                state.ledger.add_take(person, 1)

    def _load(self) -> CatState:
        """Reload the persisted memory, or start fresh if none/corrupt."""
        if not self.path.exists():
            return CatState()
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return CatState()
        state = CatState(
            mood=float(raw.get('mood', 0.0)),
            mood_day=str(raw.get('mood_day', '')),
            last_send=float(raw.get('last_send', 0.0)),
            next_session_at=float(
                raw.get('next_session_at', raw.get('next_earliest', 0.0))
            ),
            session_start_at=float(raw.get('session_start_at', 0.0)),
            session_last_at=float(raw.get('session_last_at', 0.0)),
            cat_last={
                str(k): float(v)
                for k, v in (raw.get('cat_last') or {}).items()
            },
            catted={str(p) for p in (raw.get('catted') or [])},
            posts=[(int(c), int(m)) for c, m in (raw.get('posts') or [])],
            pending=[dict(p) for p in (raw.get('pending') or [])],
            alive={
                str(k): float(v) for k, v in (raw.get('alive') or {}).items()
            },
            alive_ts=float(raw.get('alive_ts', 0.0)),
            ledger=relationship.Ledger(
                offered=relationship.int_map(raw.get('commented')),
                taken=relationship.int_map(raw.get('engaged')),
                recip=relationship.int_map(raw.get('stickered')),
                take_day=str(raw.get('engage_day', '')),
                take_today=int(raw.get('engage_today', 0)),
                recip_day=str(raw.get('sticker_day', '')),
                recip_today=int(raw.get('sticker_today', 0)),
                names={
                    str(k): str(v)
                    for k, v in (raw.get('names') or {}).items()
                },
            ),
        )
        self._backfill_ledger(state)
        return state

    def _save(self) -> None:
        """Persist the memory atomically as readable JSON."""
        data = {
            'mood': self.state.mood,
            'mood_day': self.state.mood_day,
            'last_send': self.state.last_send,
            'next_session_at': self.state.next_session_at,
            'session_start_at': self.state.session_start_at,
            'session_last_at': self.state.session_last_at,
            'cat_last': self.state.cat_last,
            'catted': sorted(self.state.catted),
            'posts': [[c, m] for c, m in self.state.posts],
            'pending': self.state.pending,
            'alive': self.state.alive,
            'alive_ts': self.state.alive_ts,
            'commented': self.state.ledger.offered,
            'engaged': self.state.ledger.taken,
            'stickered': self.state.ledger.recip,
            'engage_day': self.state.ledger.take_day,
            'engage_today': self.state.ledger.take_today,
            'sticker_day': self.state.ledger.recip_day,
            'sticker_today': self.state.ledger.recip_today,
            'names': self.state.ledger.names,
        }
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        tmp.replace(self.path)


def _emoji(raw: dict[str, object]) -> CatEmoji:
    """One CatEmoji from its JSON dict (unknown keys ignored)."""
    tags = raw.get('tags') or []
    return CatEmoji(
        emoji_id=str(raw.get('id', '')),
        fallback=str(raw.get('fallback') or ' '),
        base=float(raw.get('base') or 1.0),
        tags=tuple(str(t) for t in tags),
    )


def _peaks(
    value: object,
) -> tuple[tuple[float, float, float], ...]:
    """Parse a JSON list of [mean, sigma, weight] peaks."""
    rows = value if isinstance(value, list) else []
    out = []
    for row in rows:
        mean, sigma, weight = row
        out.append((float(mean), float(sigma), float(weight)))
    return tuple(out)


def _entries_of_type(data: dict[str, object], etype: str) -> list[dict]:
    """Return the emoji dicts of one ``type`` from the top-level array."""
    top = data.get('emoji')
    if not isinstance(top, list):
        return []
    return [e for e in top if isinstance(e, dict) and e.get('type') == etype]


def load_cat_params(data: dict[str, object]) -> CatParams:
    """Load the cat engine's parameters from the constants JSON 'cats' key."""
    cats = data.get('cats') if isinstance(data.get('cats'), dict) else {}
    cats = cats or {}
    pool = tuple(_emoji(dict(e)) for e in _entries_of_type(data, 'cat'))
    like_pool = tuple(_emoji(dict(e)) for e in _entries_of_type(data, 'like'))
    return CatParams(
        enabled=bool(cats.get('enabled', False)),
        like_all=bool(cats.get('like_all', False)),
        comments_in_discussion=bool(cats.get('comments_in_discussion', False)),
        react_to_posts=bool(cats.get('react_to_posts', False)),
        watch_posts=int(cats.get('watch_posts') or 4),
        hours_weekday=_peaks(cats.get('hours_weekday'))
        or ((9.0, 2.0, 1.0), (21.0, 2.5, 1.3)),
        hours_weekend=_peaks(cats.get('hours_weekend'))
        or ((11.0, 3.0, 1.0), (22.0, 3.0, 1.2)),
        quiet_hours=frozenset(
            int(h) for h in (cats.get('quiet_hours') or [2, 3, 4, 5, 6])
        ),
        active_start=float(cats.get('active_start_hour', 0.0)),
        active_end=float(cats.get('active_end_hour', 24.0)),
        uptime_half_life_sec=float(cats.get('uptime_half_life_sec', 864000.0)),
        uptime_learn_obs=float(cats.get('uptime_learn_obs', 2000.0)),
        skip_if_manually_replied=bool(
            cats.get('skip_if_manually_replied', True)
        ),
        tz_offset_hours=float(cats.get('tz_offset_hours', 3.0)),
        latency_log_mu=float(cats.get('latency_log_mu', 7.0)),
        latency_log_sigma=float(cats.get('latency_log_sigma', 1.2)),
        spacing_log_mu=float(cats.get('spacing_log_mu', 9.5)),
        spacing_log_sigma=float(cats.get('spacing_log_sigma', 1.3)),
        jitter_sec=float(cats.get('jitter_sec', 90.0)),
        skip_prob=float(cats.get('skip_prob', 0.12)),
        double_prob=float(cats.get('double_prob', 0.06)),
        double_gap_sec=float(cats.get('double_gap_sec', 40.0)),
        silent_day_prob=float(cats.get('silent_day_prob', 0.08)),
        recency_half_life_sec=float(
            cats.get('recency_half_life_sec', 172800.0)
        ),
        mood_phi=float(cats.get('mood_phi', 0.8)),
        mood_sigma=float(cats.get('mood_sigma', 0.35)),
        feedback_speedup=float(cats.get('feedback_speedup', 0.4)),
        session_gap_log_mu=float(cats.get('session_gap_log_mu', 3.8)),
        session_gap_log_sigma=float(cats.get('session_gap_log_sigma', 0.6)),
        session_idle_sec=float(cats.get('session_idle_sec', 900.0)),
        session_max_sec=float(cats.get('session_max_sec', 1200.0)),
        max_reply_delay_sec=float(cats.get('max_reply_delay_sec', 21600.0)),
        pool=pool,
        like_pool=like_pool,
        rescan_sec=float(cats.get('rescan_sec', 300.0)),
        attach_enabled=bool(cats.get('attach_enabled', True)),
        exposure_c1=float(cats.get('exposure_c1', 0.45)),
        exposure_c2=float(cats.get('exposure_c2', 0.90)),
        exposure_k=float(cats.get('exposure_k', 8.0)),
        like_control_gain=float(cats.get('like_control_gain', 1.0)),
        recip_fraction_target=float(cats.get('recip_fraction_target', 0.20)),
        recip_control_gain=float(cats.get('recip_control_gain', 1.0)),
        like_max_per_day=int(cats.get('like_max_per_day', 400)),
        sticker_max_per_day=int(cats.get('sticker_max_per_day', 40)),
    )
