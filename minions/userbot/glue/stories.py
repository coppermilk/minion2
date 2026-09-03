# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Human-like story viewing: the Telethon side of the story brain.

A collaborator, not a mixin. It owns what is genuinely its own -- the poll
loop, the planned-view queue that /status reads, and the timers each view
runs on -- and reaches the rest through ``StoryDeps``: the client, the brain
that decides, the chat to report into, and one resolver for a peer's @name.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from minion_core.adapters import userchat
from minions.userbot.core import tasks
from minions.userbot.core.models import iso
from minions.userbot.engines import stories

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable


log = logging.getLogger('userbot')

# How many recent views the /stories report lists.
REPORT_ROWS = 10


@dataclass(frozen=True)
class StoryDeps:
    """Everything the story watcher may reach; nothing else is in scope."""

    account: userchat.Account
    brain: stories.StoryBrain
    source: int  # where /stories reports
    learn: Callable[[int], Awaitable[str]]  # resolve a peer, store, name it
    name: Callable[[int], str]  # what we already know a peer as


@dataclass
class StoryWatch:
    """Poll the stories feed and view a human-like handful, on timers."""

    deps: StoryDeps
    # Planned-but-not-yet-fired views, and the next poll -- both read by
    # /status, so they live here rather than on the host.
    pending: list[stories.StoryView] = field(default_factory=list)
    next_poll: float = 0.0
    # Reactions this profile planned and could not place, so "0 reacted"
    # can be told from "every one was refused". In memory: a diagnostic
    # about the run, worthless after a restart.
    unplaced: int = 0
    _views: set[asyncio.Task[None]] = field(default_factory=set)

    def cancel(self) -> None:
        """Drop every in-flight view timer and forget the queue."""
        tasks.cancel_all(self._views)
        self.pending.clear()

    async def loop(self) -> None:
        """Periodically poll the stories feed and view a human-like handful.

        Telegram's own stories feed already limits this to contacts / people
        we follow, so a poll only ever sees friends' stories. Each pass fetches
        the feed, keeps the peers with UNSEEN stories, and lets the brain plan
        a small, human-paced session; each planned view runs on its own timer
        (``_view_later``). No reactions are ever sent -- just a view and a log.
        Off when disabled or the poll period is <= 0.
        """
        period = self.deps.brain.params.poll_sec
        if not self.deps.brain.params.enabled or period <= 0:
            return
        while True:
            self.next_poll = time.time() + period
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('stories: poll failed; will retry')
            await asyncio.sleep(period)

    async def _poll_once(self) -> None:
        """Fetch the feed, plan a session, and arm a timer per planned view."""
        candidates = await self._candidates()
        if not candidates:
            log.info('stories: no peers with unseen stories this poll')
            return
        views = self.deps.brain.plan(candidates)
        if not views:
            reason = (
                self.deps.brain.blocked_reason() or 'all skipped this glance'
            )
            log.info(
                'stories: %d peer(s) with stories, queued 0 (%s)',
                len(candidates),
                reason,
            )
            return
        for view in views:
            self.pending.append(view)  # shown as the /status queue
            tasks.spawn(self._views, self._view_later(view))
        log.info(
            'stories: %d peer(s) with stories, queued %d',
            len(candidates),
            len(views),
        )

    async def _candidates(self) -> list[stories.StoryCandidate]:
        """Return every peer the feed shows with stories up right now.

        Reads Telegram's active-stories feed (contacts / followed peers
        only) and turns each peer into a ``StoryCandidate``. When
        ``include_archived`` is on, the hidden feed (people whose chats
        were moved to the Archive) is polled too and merged in, deduped
        by peer -- and each candidate remembers which feed it came from,
        because the operator sees those as two separate rows in Telegram.
        """
        out: dict[int, stories.StoryCandidate] = {}
        await self._collect_feed(out, hidden=False)
        if self.deps.brain.params.include_archived:
            await self._collect_feed(out, hidden=True)
        return list(out.values())

    async def _collect_feed(
        self, out: dict[int, stories.StoryCandidate], *, hidden: bool
    ) -> None:
        """Read one stories feed (main or hidden) into ``out``, keyed by peer.

        ``hidden`` selects the archived-contacts feed; a peer already
        collected from the other feed is not overwritten.

        EVERYONE with stories up is collected, including peers whose
        stories we have already seen. The planner filters those out again
        a moment later (``_eligible``), so nothing about what we view
        changes -- but /status can now say something about every person
        the operator can see in Telegram, instead of going quiet about
        the ones we are not opening.
        """
        which = 'hidden' if hidden else 'main'
        feed = await self.deps.account.stories_feed(hidden=hidden)
        fresh = 0
        for peer_stories in feed:
            cand = self._candidate(peer_stories, hidden=hidden)
            if cand is not None and cand.peer_id not in out:
                out[cand.peer_id] = cand
                fresh += bool(self.deps.brain.unseen(cand))
        log.info(
            'stories: %s feed has %d peer(s) with stories, %d new-to-us',
            which,
            len(feed),
            fresh,
        )

    def _candidate(
        self, peer_stories: userchat.PeerStories, *, hidden: bool = False
    ) -> stories.StoryCandidate | None:
        """Build a ``StoryCandidate`` from one feed entry, or None if empty."""
        ids = tuple(story.id for story in peer_stories.stories)
        if not ids:
            return None
        return stories.StoryCandidate(
            hidden=hidden,
            peer_id=peer_stories.peer_id,
            story_ids=ids,
            max_id=max(ids),
            last_ts=max(
                (story.date for story in peer_stories.stories), default=0.0
            ),
        )

    async def _view_later(self, view: stories.StoryView) -> None:
        """Sleep until the view is due, then open the stories and mark them.

        Only what Telegram actually opened is marked seen. A story can expire
        between the plan and the send, and marking that one seen would record
        a view that never happened -- inflating the peer's exposure and
        burning the id for good. It stays unseen instead: if it really is
        gone, the live feed no longer offers it and it simply disappears; if
        the failure was transient, the next poll picks it up again.

        This is what the module always claimed to do, via ``except`` around
        the watch. That path is close to dead: the adapter's contract is to
        return False rather than raise, so a wholly refused view completed
        "successfully" and everything got marked anyway.
        """
        delay = view.when - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        self._dequeue(view)  # it is firing now: drop it from the queue
        try:
            opened = await self._watch(view)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('stories: view failed for %s', view.peer_id)
            return
        if not opened:
            log.warning(
                'stories: %s opened none of %d; leaving them unseen',
                self._who(view.peer_id),
                len(view.story_ids),
            )
            return
        name = await self.deps.learn(view.peer_id)
        self.deps.brain.mark_viewed(view.peer_id, opened)
        log.info('stories: viewed %d of %s', len(opened), name)

    def _dequeue(self, view: stories.StoryView) -> None:
        """Drop a fired view from the /status queue (no-op if already gone)."""
        with contextlib.suppress(ValueError):
            self.pending.remove(view)

    async def _watch(self, view: stories.StoryView) -> tuple[int, ...]:
        """Open each chosen story (human dwell), react to some, mark read.

        Opens the stories one at a time with a short random dwell between them
        (a person does not blink through a whole set instantly), incrementing
        each story's view counter and leaving an occasional heart/thumb on the
        ids the brain chose (``react_ids``), then marks the peer read up to
        ``max_id``. Reactions are recorded (against the daily cap) as they go.

        Returns the ids Telegram actually opened. One it refuses is skipped
        whole -- no reaction is placed on a story we never saw -- and the
        caller leaves it unseen.
        """
        peer = await self.deps.account.input_peer(view.peer_id)
        params = self.deps.brain.params
        react_set = set(view.react_ids)
        opened: list[int] = []
        reacted: list[int] = []
        for sid in view.story_ids:
            await asyncio.sleep(
                random.uniform(  # noqa: S311 -- human dwell, not crypto
                    params.dwell_min_sec, params.dwell_max_sec
                )
            )
            if not await self.deps.account.view_story(peer, sid):
                continue
            opened.append(sid)
            if sid in react_set and await self.deps.account.react_to_story(
                peer, sid, view.react_emoji
            ):
                reacted.append(sid)
        await self.deps.account.read_stories(peer, view.max_id)
        if reacted:
            self.deps.brain.mark_reacted(
                view.peer_id, tuple(reacted), time.time()
            )
        self._note_unplaced(view, len(react_set), len(reacted))
        return tuple(opened)

    def _note_unplaced(
        self, view: stories.StoryView, planned: int, sent: int
    ) -> None:
        """Count reactions that were planned and did not go out.

        The engine records only what LANDED, so a batch Telegram refused and
        a batch that was never planned leave the same trace: none. Counted
        here and shown in /status, the two stop looking alike.
        """
        lost = planned - sent
        if lost <= 0:
            return
        self.unplaced += lost
        log.warning(
            'stories: %d of %d planned reaction(s) to %s did not go out',
            lost,
            planned,
            self._who(view.peer_id),
        )

    def _who(self, peer_id: int) -> str:
        """Name a peer for a log line, falling back to their bare id."""
        return self.deps.name(peer_id) or str(peer_id)

    async def report(self) -> None:
        """Post the story-viewer log to the source chat (/stories command)."""
        await self.deps.account.send(
            self.deps.source, userchat.Text(self.text())
        )
        log.info('sent stories report to %s', self.deps.source)

    def text(self) -> str:
        """Return the /stories message: total viewed and the recent views."""
        if not self.deps.brain.params.enabled:
            return 'Story viewer: disabled (set stories.enabled in the JSON).'
        lines = [
            f'Story viewer: {self.deps.brain.seen_count()} viewed all-time'
        ]
        recent = self.deps.brain.recent_log(REPORT_ROWS)
        if recent:
            lines.append('  recent views:')
            lines += [
                f'    - {self._who(e.peer_id)}: {e.count} story(s) {iso(e.ts)}'
                for e in recent
            ]
        else:
            lines.append('  (nothing viewed yet)')
        return '\n'.join(lines)
