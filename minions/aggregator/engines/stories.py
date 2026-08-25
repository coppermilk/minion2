# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Human-like viewing of friends' and contacts' stories (no reactions).

The aggregator's user account also *watches stories*, the way a person idly
would: it opens Telegram now and then, glances at a handful of the newest
unseen stories from people it follows, marks them seen, and closes the app --
it never leaves NO reaction, never comments, never likes. Just a view, plus a
log of whose stories were watched.

Like ``cats.py`` this module is deliberately Telethon-free (pure Python +
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
5. State is persisted (the seen set, the session cursor, a rolling view log),
   so a restart keeps its memory and never double-counts.

All human-visible text (none is needed here) would live in the constants JSON;
this source stays ASCII.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from minions.aggregator.core import humanize_time

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@dataclass(frozen=True)
class StoryParams:
    """Every tunable, loaded from the constants JSON 'stories' section."""

    enabled: bool
    # Catch-up mode: view EVERY unseen story each poll -- no per-session cap,
    # no skip, no long cooldown between sessions -- so all currently-active
    # stories are swept at once instead of a slow human trickle. Quiet hours
    # and the odd silent day still apply, and the per-story dwell / small gap
    # between peers stay (viewing dozens instantly would trip Telegram's flood
    # limits). Off is the human-like handful-then-rest behaviour.
    view_all: bool
    # Also view the stories of people whose CHATS were moved to the Archive
    # (Telegram's "hidden" stories feed). Off keeps to the main feed only --
    # archived contacts are left alone. ``main.py`` polls the hidden feed too
    # when this is on; the brain treats both feeds' peers the same.
    include_archived: bool
    # The persona's UTC offset: quiet hours / the silent-day date are read in
    # THIS timezone, so the cadence matches the human, not the server clock.
    tz_offset_hours: float
    quiet_hours: frozenset[int]  # local hours with no viewing (asleep)
    # How often ``main.py`` re-polls the stories feed (test tight, live
    # relaxed; both default via ``poll_sec``). Carried for main, not the brain.
    poll_sec: float
    # One session views between min and max peers -- a glance, not the whole
    # feed. Picked per session so the count itself varies.
    per_session_min: int
    per_session_max: int
    skip_peer_prob: float  # chance an eligible peer is skipped this pass
    silent_day_prob: float  # chance a whole day views nothing
    # First view lands a short, human "just opened the app" beat after the
    # plan; gap_* is the lognormal pause between one peer and the next in a
    # session; spacing_* is the long, heavy-tailed gap to the NEXT session.
    latency_log_mu: float
    latency_log_sigma: float
    gap_log_mu: float
    gap_log_sigma: float
    spacing_log_mu: float
    spacing_log_sigma: float
    # How long to linger on each individual story (main.py dwells this long
    # between marking one story and the next, so a peer's set is not opened in
    # a single machine-fast blink). Carried for main; the brain does not sleep.
    dwell_min_sec: float
    dwell_max_sec: float
    max_peers_tracked: int  # cap the persisted seen map (drop oldest peers)
    seen_per_peer: int  # cap the per-peer seen id list (newest kept)
    log_limit: int  # how many recent views to keep for /status and /stories
    catch_up_max: int  # cap on peers viewed per poll in view_all (anti-binge)


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
    label: str = ''  # @name / title, for the log (never shown to the peer)


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
    label: str = ''


@dataclass
class StoryState:
    """The persisted memory: what we have already seen, and the session cursor.

    ``seen`` maps a peer id (as str, for JSON) to the story ids already viewed,
    so principle 1 (never re-view) survives a restart. ``log`` is a rolling
    record of recent views for /status and /stories.
    """

    seen: dict[str, list[int]] = field(default_factory=dict)
    seen_order: list[str] = field(default_factory=list)  # LRU of peer ids
    last_view: float = 0.0
    next_session_at: float = 0.0
    session_start_at: float = 0.0
    session_last_at: float = 0.0
    total_views: int = 0  # peers viewed all-time (a simple odometer)
    log: list[dict[str, object]] = field(default_factory=list)


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
        path: Path,
        rng: random.Random | None = None,
    ) -> None:
        """Bind the params and state path; seed the RNG; load the memory."""
        self.params = params
        self.path = path
        self.rng = rng or random.Random()  # noqa: S311 -- mimicry, not crypto
        self.clock = time.time
        self.state = self._load()

    def unseen(self, cand: StoryCandidate) -> tuple[int, ...]:
        """Return the candidate's story ids we have not viewed yet."""
        seen = set(self.state.seen.get(str(cand.peer_id), ()))
        return tuple(sid for sid in cand.story_ids if sid not in seen)

    def plan(
        self, candidates: list[StoryCandidate], now: float | None = None
    ) -> list[StoryView]:
        """Plan this pass's views: a human glance, or nothing.

        Returns an empty list -- view nothing -- when disabled, in quiet hours,
        on a silent day, or still inside the between-session cooldown.
        Otherwise picks a small, freshest-first, partly-skipped subset of the
        peers with unseen stories, staggered across a short viewing session.
        Marks nothing seen; ``mark_viewed`` does that once a read succeeds.
        """
        now = self.clock() if now is None else now
        if not self._session_open(now):
            return []
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
        if not chosen:
            return []
        return self._lay_out(chosen, now)

    def _session_open(self, now: float) -> bool:
        """Return whether a viewing session may run right now."""
        return self.blocked_reason(now) is None

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
        if humanize_time.in_quiet_hours(now, tz, self.params.quiet_hours):
            return 'quiet-hours'
        if humanize_time.is_silent_day(now, tz, self.params.silent_day_prob):
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

    def _lay_out(
        self,
        chosen: list[tuple[StoryCandidate, tuple[int, ...]]],
        now: float,
    ) -> list[StoryView]:
        """Stagger the chosen peers across one session and set the cursors.

        The first view lands a short "just opened the app" beat from now; each
        next peer follows a lognormal gap later. The between-session cursor is
        pushed a long, heavy-tailed spacing past the last view, so the next
        session is a proper while away (principle 2).
        """
        when = now + humanize_time.lognormal(
            self.rng, self.params.latency_log_mu, self.params.latency_log_sigma
        )
        self.state.session_start_at = when
        views: list[StoryView] = []
        for cand, unseen in chosen:
            views.append(
                StoryView(
                    peer_id=cand.peer_id,
                    story_ids=unseen,
                    max_id=cand.max_id,
                    when=when,
                    label=cand.label,
                )
            )
            when += humanize_time.lognormal(
                self.rng, self.params.gap_log_mu, self.params.gap_log_sigma
            )
        self.state.session_last_at = when
        # The between-session cursor is pushed a heavy-tailed spacing past the
        # last view -- for catch-up (view_all) TOO, so a backlog is cleared a
        # session's worth at a time at the human rhythm rather than swept every
        # poll (which dumped a whole night's stories in one morning burst).
        spacing = humanize_time.lognormal(
            self.rng,
            self.params.spacing_log_mu,
            self.params.spacing_log_sigma,
        )
        self.state.next_session_at = when + spacing
        self._save()
        return views

    def mark_viewed(  # noqa: PLR0913 -- id + ids + optional label/ts read best flat
        self,
        peer_id: int,
        story_ids: tuple[int, ...],
        *,
        label: str = '',
        ts: float | None = None,
    ) -> None:
        """Record that ``story_ids`` of ``peer_id`` were viewed (persisted).

        Idempotent: re-marking an already-seen id changes nothing. Keeps the
        per-peer seen list and the tracked-peer map bounded, appends one line
        to the rolling view log, and advances the odometer by the number of
        newly-seen ids (so a re-mark does not inflate the count).
        """
        now = self.clock() if ts is None else ts
        key = str(peer_id)
        prior = self.state.seen.get(key, [])
        known = set(prior)
        fresh = [sid for sid in story_ids if sid not in known]
        if not fresh:
            return
        merged = prior + fresh
        self.state.seen[key] = merged[-self.params.seen_per_peer :]
        self._touch_peer(key)
        self.state.last_view = now
        self.state.total_views += len(fresh)
        self.state.log.append(
            {
                'peer_id': peer_id,
                'label': label,
                'count': len(fresh),
                'ts': now,
            }
        )
        del self.state.log[: -self.params.log_limit]
        self._save()

    def _touch_peer(self, key: str) -> None:
        """Mark ``key`` most-recently-seen, dropping the oldest past cap."""
        if key in self.state.seen_order:
            self.state.seen_order.remove(key)
        self.state.seen_order.append(key)
        while len(self.state.seen_order) > self.params.max_peers_tracked:
            oldest = self.state.seen_order.pop(0)
            self.state.seen.pop(oldest, None)

    def recent_log(self, limit: int) -> list[dict[str, object]]:
        """Return recent views, newest first (for /status, /stories)."""
        return list(reversed(self.state.log))[:limit]

    def seen_count(self) -> int:
        """Return how many stories have been viewed all-time (the odometer)."""
        return self.state.total_views

    def views_today(self, now: float, tz: float) -> int:
        """Return how many stories were viewed on the local date of ``now``.

        Summed from the view log (each entry carries its ``ts`` and ``count``),
        so it resets naturally at local midnight -- what /status shows instead
        of the all-time odometer.
        """
        today = humanize_time.local(now, tz).date()
        total = 0
        for entry in self.state.log:
            ts = entry.get('ts')
            if isinstance(ts, int | float) and (
                humanize_time.local(ts, tz).date() == today
            ):
                total += int(entry.get('count', 0) or 0)
        return total

    def _load(self) -> StoryState:
        """Reload the persisted memory, or start fresh if none/corrupt."""
        if not self.path.exists():
            return StoryState()
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return StoryState()
        seen = {
            str(k): [int(x) for x in v]
            for k, v in (raw.get('seen') or {}).items()
        }
        order = [
            str(k)
            for k in (raw.get('seen_order') or [])
            if str(k) in seen
        ]
        # Any peer missing from the persisted order (older state file) is
        # appended, so the LRU cap still has every tracked peer to work with.
        order += [k for k in seen if k not in order]
        return StoryState(
            seen=seen,
            seen_order=order,
            last_view=float(raw.get('last_view', 0.0)),
            next_session_at=float(raw.get('next_session_at', 0.0)),
            session_start_at=float(raw.get('session_start_at', 0.0)),
            session_last_at=float(raw.get('session_last_at', 0.0)),
            total_views=int(raw.get('total_views', 0)),
            log=[dict(e) for e in (raw.get('log') or [])],
        )

    def _save(self) -> None:
        """Persist the memory atomically as readable JSON."""
        data = {
            'seen': self.state.seen,
            'seen_order': self.state.seen_order,
            'last_view': self.state.last_view,
            'next_session_at': self.state.next_session_at,
            'session_start_at': self.state.session_start_at,
            'session_last_at': self.state.session_last_at,
            'total_views': self.state.total_views,
            'log': self.state.log,
        }
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        tmp.replace(self.path)


def load_story_params(
    data: dict[str, object], mode: str = 'live'
) -> StoryParams:
    """Load the stories engine's parameters from the JSON 'stories' key.

    ``mode`` selects the re-poll cadence (test tight, live relaxed), mirroring
    the cats rescan interval; both fall back to ``poll_sec``.
    """
    cfg = data.get('stories') if isinstance(data.get('stories'), dict) else {}
    cfg = cfg or {}
    default_poll = float(cfg.get('poll_sec', 1800.0))
    poll_key = 'poll_sec_test' if mode == 'test' else 'poll_sec_live'
    return StoryParams(
        enabled=bool(cfg.get('enabled', False)),
        view_all=bool(cfg.get('view_all', False)),
        include_archived=bool(cfg.get('include_archived', False)),
        tz_offset_hours=float(cfg.get('tz_offset_hours', 3.0)),
        catch_up_max=int(cfg.get('catch_up_max') or 12),
        quiet_hours=frozenset(
            int(h) for h in (cfg.get('quiet_hours') or [1, 2, 3, 4, 5, 6, 7])
        ),
        poll_sec=float(cfg.get(poll_key, default_poll)),
        per_session_min=int(cfg.get('per_session_min', 2)),
        per_session_max=int(cfg.get('per_session_max', 6)),
        skip_peer_prob=float(cfg.get('skip_peer_prob', 0.35)),
        silent_day_prob=float(cfg.get('silent_day_prob', 0.06)),
        latency_log_mu=float(cfg.get('latency_log_mu', 2.5)),
        latency_log_sigma=float(cfg.get('latency_log_sigma', 0.8)),
        gap_log_mu=float(cfg.get('gap_log_mu', 2.3)),
        gap_log_sigma=float(cfg.get('gap_log_sigma', 0.7)),
        spacing_log_mu=float(cfg.get('spacing_log_mu', 8.6)),
        spacing_log_sigma=float(cfg.get('spacing_log_sigma', 1.0)),
        dwell_min_sec=float(cfg.get('dwell_min_sec', 2.0)),
        dwell_max_sec=float(cfg.get('dwell_max_sec', 9.0)),
        max_peers_tracked=int(cfg.get('max_peers_tracked', 500)),
        seen_per_peer=int(cfg.get('seen_per_peer', 40)),
        log_limit=int(cfg.get('log_limit', 50)),
    )
