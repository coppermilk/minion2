# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Human-like story viewing, mixed into Aggregator.

Extracted from ``main``: the story-feed poll loop, candidate collection, the
paced view scheduler, and the /stories report. ``_StoriesMixin`` is mixed
into ``Aggregator`` with method bodies unchanged, so they keep reading
``self`` state; the TYPE_CHECKING block declares that state for mypy.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from typing import TYPE_CHECKING

from telethon import utils
from telethon.tl.functions.stories import GetAllStoriesRequest
from telethon.tl.functions.stories import IncrementStoryViewsRequest
from telethon.tl.functions.stories import ReadStoriesRequest

from minions.aggregator import stories
from minions.aggregator.models import _iso
from minions.aggregator.models import _story_epoch

if TYPE_CHECKING:
    from telethon import TelegramClient

    from minions.aggregator.models import Config

log = logging.getLogger('aggregator')


class _StoriesMixin:
    """Story viewing, mixed into Aggregator (reads its state)."""

    if TYPE_CHECKING:  # attributes/methods provided by Aggregator (or peers)
        client: TelegramClient
        config: Config
        stories: stories.StoryBrain
        _pending_views: list[stories.StoryView]
        _story_next_poll: float
        _story_tasks: set[asyncio.Task[None]]

        async def _chat_label(self, chat_id: int) -> str: ...

        def _stories_line(self) -> str: ...

        def _stories_queue_lines(
            self, labels: dict[int, str]
        ) -> list[str]: ...

    async def stories_loop(self) -> None:
        """Periodically poll the stories feed and view a human-like handful.

        Telegram's own stories feed already limits this to contacts / people
        we follow, so a poll only ever sees friends' stories. Each pass fetches
        the feed, keeps the peers with UNSEEN stories, and lets the brain plan
        a small, human-paced session; each planned view runs on its own timer
        (``_view_later``). No reactions are ever sent -- just a view and a log.
        Off when disabled or the poll period is <= 0.
        """
        period = self.stories.params.poll_sec
        if not self.stories.params.enabled or period <= 0:
            return
        while True:
            self._story_next_poll = time.time() + period
            try:
                await self._poll_stories_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('stories: poll failed; will retry')
            await asyncio.sleep(period)

    async def _poll_stories_once(self) -> None:
        """Fetch the feed, plan a session, and arm a timer per planned view."""
        candidates = await self._fetch_story_candidates()
        if not candidates:
            log.info('stories: no peers with unseen stories this poll')
            return
        views = self.stories.plan(candidates)
        if not views:
            reason = self.stories.blocked_reason() or 'all skipped this glance'
            log.info(
                'stories: %d peer(s) with unseen stories, queued 0 (%s)',
                len(candidates),
                reason,
            )
            return
        for view in views:
            self._pending_views.append(view)  # shown as the /status queue
            task = asyncio.create_task(self._view_later(view))
            self._story_tasks.add(task)
            task.add_done_callback(self._story_tasks.discard)
        log.info(
            'stories: %d peer(s) with unseen stories, queued %d',
            len(candidates),
            len(views),
        )

    async def _fetch_story_candidates(self) -> list[stories.StoryCandidate]:
        """Return the feed's peers that still have unseen stories.

        Reads Telegram's active-stories feed (contacts / followed peers only),
        turns each peer into a ``StoryCandidate`` and keeps just those with at
        least one story id past our persisted seen set -- so we pick up what is
        genuinely new instead of walking the whole contact list. When
        ``include_archived`` is on, the hidden feed (people whose chats were
        moved to the Archive) is polled too and merged in, deduped by peer.
        """
        out: dict[int, stories.StoryCandidate] = {}
        await self._collect_story_feed(out, hidden=False)
        if self.stories.params.include_archived:
            await self._collect_story_feed(out, hidden=True)
        return list(out.values())

    async def _collect_story_feed(
        self, out: dict[int, stories.StoryCandidate], *, hidden: bool
    ) -> None:
        """Read one stories feed (main or hidden) into ``out``, keyed by peer.

        ``hidden`` selects the archived-contacts feed. Unseen-only; a peer
        already collected from the other feed is not overwritten.
        """
        which = 'hidden' if hidden else 'main'
        try:
            res = await self.client(GetAllStoriesRequest(hidden=hidden))
        except Exception:  # noqa: BLE001 -- feed unreachable: skip this pass
            log.warning('stories: could not read the %s stories feed', which)
            return
        feed = getattr(res, 'peer_stories', None) or []
        added = 0
        for peer_stories in feed:
            cand = self._story_candidate(peer_stories)
            if (
                cand is not None
                and cand.peer_id not in out
                and self.stories.unseen(cand)
            ):
                out[cand.peer_id] = cand
                added += 1
        log.info(
            'stories: %s feed has %d peer(s) with stories, %d new-to-us',
            which,
            len(feed),
            added,
        )

    def _story_candidate(
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
        dates = [_story_epoch(getattr(s, 'date', None)) for s in items]
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
        self._dequeue_view(view)  # it is firing now: drop it from the queue
        try:
            await self._view_stories(view)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('stories: view failed for %s', view.peer_id)
            return
        label = await self._chat_label(view.peer_id)
        self.stories.mark_viewed(view.peer_id, view.story_ids, label=label)
        log.info(
            'stories: viewed %d of %s', len(view.story_ids), label
        )

    def _dequeue_view(self, view: stories.StoryView) -> None:
        """Drop a fired view from the /status queue (no-op if already gone)."""
        with contextlib.suppress(ValueError):
            self._pending_views.remove(view)

    async def _view_stories(self, view: stories.StoryView) -> None:
        """Open each unseen story (human dwell), then mark the peer read.

        Opens the stories one at a time with a short random dwell between them
        (a person does not blink through a whole set instantly), incrementing
        each story's view counter, then marks the peer read up to ``max_id`` --
        the authoritative "seen" signal. Never sends a reaction.
        """
        peer = await self.client.get_input_entity(view.peer_id)
        params = self.stories.params
        for sid in view.story_ids:
            await asyncio.sleep(
                random.uniform(  # noqa: S311 -- human dwell, not crypto
                    params.dwell_min_sec, params.dwell_max_sec
                )
            )
            try:
                await self.client(
                    IncrementStoryViewsRequest(peer=peer, id=[sid])
                )
            except Exception:  # noqa: BLE001 -- best-effort; ReadStories marks
                log.debug('stories: increment view failed for %s', sid)
        await self.client(ReadStoriesRequest(peer=peer, max_id=view.max_id))

    async def stories_report(self) -> None:
        """Post the story-viewer log to the source chat (/stories command)."""
        await self.client.send_message(
            self.config.source, self._stories_text()
        )
        log.info('sent stories report to %s', self.config.source)

    def _stories_text(self) -> str:
        """Return the /stories message: total viewed and the recent views."""
        if not self.stories.params.enabled:
            return 'Story viewer: disabled (set stories.enabled in the JSON).'
        lines = [f'Story viewer: {self.stories.seen_count()} viewed all-time']
        recent = self.stories.recent_log(10)
        if recent:
            lines.append('  recent views:')
            lines += [
                f'    - {e.get("label") or e.get("peer_id")}:'
                f' {e.get("count")} story(s)'
                f' {_iso(float(str(e.get("ts", 0))))}'
                for e in recent
            ]
        else:
            lines.append('  (nothing viewed yet)')
        return '\n'.join(lines)
