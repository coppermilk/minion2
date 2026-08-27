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

from telethon import utils
from telethon.tl.functions.stories import GetAllStoriesRequest
from telethon.tl.functions.stories import IncrementStoryViewsRequest
from telethon.tl.functions.stories import ReadStoriesRequest
from telethon.tl.functions.stories import SendReactionRequest
from telethon.tl.types import ReactionEmoji

from minions.userbot.core import tasks
from minions.userbot.core.models import iso
from minions.userbot.core.models import story_epoch
from minions.userbot.engines import stories

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable

    from telethon import TelegramClient

log = logging.getLogger('userbot')

# How many recent views the /stories report lists.
REPORT_ROWS = 10


@dataclass(frozen=True)
class StoryDeps:
    """Everything the story watcher may reach; nothing else is in scope."""

    client: TelegramClient
    brain: stories.StoryBrain
    source: int  # where /stories reports
    label: Callable[[int], Awaitable[str]]  # peer id -> @name, for the log


@dataclass
class StoryWatch:
    """Poll the stories feed and view a human-like handful, on timers."""

    deps: StoryDeps
    # Planned-but-not-yet-fired views, and the next poll -- both read by
    # /status, so they live here rather than on the host.
    pending: list[stories.StoryView] = field(default_factory=list)
    next_poll: float = 0.0
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
                'stories: %d peer(s) with unseen stories, queued 0 (%s)',
                len(candidates),
                reason,
            )
            return
        for view in views:
            self.pending.append(view)  # shown as the /status queue
            tasks.spawn(self._views, self._view_later(view))
        log.info(
            'stories: %d peer(s) with unseen stories, queued %d',
            len(candidates),
            len(views),
        )

    async def _candidates(self) -> list[stories.StoryCandidate]:
        """Return the feed's peers that still have unseen stories.

        Reads Telegram's active-stories feed (contacts / followed peers only),
        turns each peer into a ``StoryCandidate`` and keeps just those with at
        least one story id past our persisted seen set -- so we pick up what is
        genuinely new instead of walking the whole contact list. When
        ``include_archived`` is on, the hidden feed (people whose chats were
        moved to the Archive) is polled too and merged in, deduped by peer.
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

        ``hidden`` selects the archived-contacts feed. Unseen-only; a peer
        already collected from the other feed is not overwritten.
        """
        which = 'hidden' if hidden else 'main'
        try:
            res = await self.deps.client(GetAllStoriesRequest(hidden=hidden))
        except Exception:  # noqa: BLE001 -- feed unreachable: skip this pass
            log.warning('stories: could not read the %s stories feed', which)
            return
        feed = getattr(res, 'peer_stories', None) or []
        added = 0
        for peer_stories in feed:
            cand = self._candidate(peer_stories)
            if (
                cand is not None
                and cand.peer_id not in out
                and self.deps.brain.unseen(cand)
            ):
                out[cand.peer_id] = cand
                added += 1
        log.info(
            'stories: %s feed has %d peer(s) with stories, %d new-to-us',
            which,
            len(feed),
            added,
        )

    def _candidate(
        self, peer_stories: object
    ) -> stories.StoryCandidate | None:
        """Build a ``StoryCandidate`` from one feed entry, or None if empty."""
        peer = getattr(peer_stories, 'peer', None)
        if peer is None:
            return None
        items = getattr(peer_stories, 'stories', None) or []
        ids = [int(getattr(s, 'id', 0) or 0) for s in items]
        ids = [sid for sid in ids if sid > 0]
        if not ids:
            return None
        dates = [story_epoch(getattr(s, 'date', None)) for s in items]
        return stories.StoryCandidate(
            peer_id=int(utils.get_peer_id(peer)),
            story_ids=tuple(ids),
            max_id=max(ids),
            last_ts=max(dates, default=0.0),
            label=str(utils.get_peer_id(peer)),
        )

    async def _view_later(self, view: stories.StoryView) -> None:
        """Sleep until the view is due, then open the stories and mark them.

        Failures are logged (not swallowed) and the peer is NOT marked seen, so
        a failed read is retried on the next poll rather than silently skipped.
        Every successful view is recorded to the persisted view log with a
        readable @name resolved here (the plan only carries the peer id).
        """
        delay = view.when - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        self._dequeue(view)  # it is firing now: drop it from the queue
        try:
            await self._watch(view)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('stories: view failed for %s', view.peer_id)
            return
        label = await self.deps.label(view.peer_id)
        self.deps.brain.mark_viewed(view.peer_id, view.story_ids, label=label)
        log.info('stories: viewed %d of %s', len(view.story_ids), label)

    def _dequeue(self, view: stories.StoryView) -> None:
        """Drop a fired view from the /status queue (no-op if already gone)."""
        with contextlib.suppress(ValueError):
            self.pending.remove(view)

    async def _watch(self, view: stories.StoryView) -> None:
        """Open each chosen story (human dwell), react to some, mark read.

        Opens the stories one at a time with a short random dwell between them
        (a person does not blink through a whole set instantly), incrementing
        each story's view counter and leaving an occasional heart/thumb on the
        ids the brain chose (``react_ids``), then marks the peer read up to
        ``max_id``. Reactions are recorded (against the daily cap) as they go.
        """
        peer = await self.deps.client.get_input_entity(view.peer_id)
        params = self.deps.brain.params
        react_set = set(view.react_ids)
        sent = 0
        for sid in view.story_ids:
            await asyncio.sleep(
                random.uniform(  # noqa: S311 -- human dwell, not crypto
                    params.dwell_min_sec, params.dwell_max_sec
                )
            )
            await self._increment_view(peer, sid)
            if sid in react_set:
                sent += await self._react_to_story(peer, sid, view.react_emoji)
        await self.deps.client(
            ReadStoriesRequest(peer=peer, max_id=view.max_id)
        )
        if sent:
            self.deps.brain.mark_reacted(view.peer_id, sent, time.time())

    async def _increment_view(self, peer: object, sid: int) -> None:
        """Register one story view (best-effort; ReadStories is the mark)."""
        try:
            await self.deps.client(
                IncrementStoryViewsRequest(peer=peer, id=[sid])
            )
        except Exception:  # noqa: BLE001 -- best-effort; ReadStories marks
            log.debug('stories: increment view failed for %s', sid)

    async def _react_to_story(self, peer: object, sid: int, emoji: str) -> int:
        """Leave one reaction on a story; return 1 on success, 0 on failure."""
        try:
            await self.deps.client(
                SendReactionRequest(
                    peer=peer,
                    story_id=sid,
                    reaction=ReactionEmoji(emoticon=emoji),
                )
            )
        except Exception:  # noqa: BLE001 -- best-effort, capped per day
            log.debug('stories: reaction failed for %s', sid)
            return 0
        return 1

    async def report(self) -> None:
        """Post the story-viewer log to the source chat (/stories command)."""
        await self.deps.client.send_message(self.deps.source, self.text())
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
                f'    - {e.get("label") or e.get("peer_id")}:'
                f' {e.get("count")} story(s)'
                f' {iso(float(str(e.get("ts", 0))))}'
                for e in recent
            ]
        else:
            lines.append('  (nothing viewed yet)')
        return '\n'.join(lines)
