# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Add the networks that came out after a post to its "watch" grid.

The Telethon side of ``core/gridfill``. The aggregator posts a video when
every network arrived or its timeout ran out (three hours); a network
published later used to be lost to the post for good. Every tick, inside
the persona's waking hours, this worker reads the site's public posts feed
(``engines.grid.feed_url``, ``/posts.json``: one sketch, its short links by
network), reads the channel's newest ``page`` posts younger than
``window_days`` plus the posts named in ``posts`` (a one-off catch-up of
older ones), and for each post whose grid lacks a network the feed has:

* ``dry`` -- logs ``grid: post N + network, ...``;
* ``on`` -- journals the post (``grid_journal.jsonl``: text and Telegram's
  own entities, enough to undo by hand), then edits it, at most
  ``per_tick`` posts a tick and ``pause_sec`` (never under 10 s) apart.

Nothing else of the post moves: the text before and after the grid, every
premium emoji, the label and underline of each cell. A feed that cannot be
read or parsed skips the tick (one log line). It keeps no state: a
completed post plans nothing, so the next tick edits nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from minion_core.adapters import feed as feeds
from minion_core.adapters import userchat
from minions.userbot.core import codec
from minions.userbot.core import gridfill
from minions.userbot.core import relink
from minions.userbot.core import tgedit
from minions.userbot.glue.tgedit import to_post

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from collections.abc import Mapping
    from pathlib import Path

log = logging.getLogger('userbot')

JOURNAL_FILE = 'grid_journal.jsonl'
FEED_URL = 'https://daetotyoma.com/posts.json'
MODES = ('off', 'dry', 'on')
DAY_SEC = 24 * 3600.0


@dataclass(frozen=True)
class GridParams:
    """The worker's knobs, from ``engines.grid``, ``post`` and ``persona``."""

    layout: gridfill.Layout
    mode: str = 'off'
    feed_url: str = FEED_URL
    window_days: float = 14.0
    page: int = 30
    posts: tuple[int, ...] = ()
    per_tick: int = 5
    tick_sec: float = 900.0
    pause_sec: float = tgedit.MIN_PAUSE_SEC
    clock: relink.Clock = field(default_factory=relink.Clock)


def _layout(settings: Mapping[str, object]) -> gridfill.Layout:
    """Read the grid's drawing from ``post`` and the platform emoji."""
    post = codec.section(settings, 'post')
    rows = tuple(
        tuple(str(n) for n in codec.rows(row))
        for row in codec.rows(post.get('rows'))
    )
    emoji: dict[str, tuple[str, str]] = {}
    for row in codec.rows(settings.get('emoji')):
        if not isinstance(row, dict) or row.get('type') != 'platform':
            continue
        name = codec.text(row.get('name'), '')
        ident = codec.text(row.get('id'), '')
        if name and ident:
            emoji[name] = (ident, codec.text(row.get('fallback'), ''))
    return gridfill.Layout(
        rows=rows,
        separator=codec.text(post.get('column_separator'), '  |  '),
        domain=codec.text(post.get('short_link_domain'), ''),
        emoji=emoji,
    )


def load_grid_params(settings: Mapping[str, object]) -> GridParams:
    """Read ``engines.grid``; an unknown mode reads as ``off``."""
    cfg = codec.engine(settings, 'grid')
    persona = codec.section(settings, 'persona')
    mode = codec.text(cfg.get('mode'), 'off')
    return GridParams(
        layout=_layout(settings),
        mode=mode if mode in MODES else 'off',
        feed_url=codec.text(cfg.get('feed_url'), FEED_URL),
        window_days=codec.num(cfg.get('window_days'), 14.0),
        page=int(codec.num(cfg.get('page'), 30)),
        posts=tuple(
            int(v) for v in codec.rows(cfg.get('posts')) if isinstance(v, int)
        ),
        per_tick=int(codec.num(cfg.get('per_tick'), 5)),
        tick_sec=codec.num(cfg.get('tick_sec'), 900.0),
        pause_sec=max(
            tgedit.MIN_PAUSE_SEC,
            codec.num(cfg.get('pause_sec'), tgedit.MIN_PAUSE_SEC),
        ),
        clock=relink.Clock(
            tz_offset_hours=codec.num(persona.get('tz_offset_hours'), 0.0),
            wake_start_hour=int(codec.num(persona.get('wake_start_hour'), 0)),
            wake_end_hour=int(codec.num(persona.get('wake_end_hour'), 24)),
        ),
    )


@dataclass(frozen=True)
class GridDeps:
    """Everything the worker may reach; nothing else is in scope."""

    account: userchat.Account
    channel: Callable[[], int]  # the profile's channel (test or live)
    feed: Callable[[], Awaitable[str]]  # the posts feed's text
    journal: Path  # grid_journal.jsonl
    params: GridParams
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep


@dataclass
class GridKeeper:
    """Keep the posts' grids as complete as the site's posts feed."""

    deps: GridDeps

    async def loop(self) -> None:
        """Tick forever; one failed tick is logged and retried later."""
        while True:
            try:
                await self.tick(time.time())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('grid: tick failed; will retry')
            await asyncio.sleep(self.deps.params.tick_sec)

    async def tick(self, now: float) -> list[gridfill.Plan]:
        """Plan (and in ``on`` mode apply) every incomplete post.

        Returns the plans that add a network, applied or not; past
        ``per_tick`` they wait for the next tick.
        """
        params = self.deps.params
        if params.mode == 'off' or not params.clock.awake(now):
            return []
        try:
            feed = gridfill.parse_feed(await self.deps.feed())
        except (feeds.FeedUnreachableError, gridfill.FeedError) as exc:
            log.warning('grid: posts feed skipped this tick: %s', exc)
            return []
        due = self._due(await self._posts(now), feed)
        for writes, (post, got) in enumerate(due[: params.per_tick]):
            await self._apply(post, got, writes=writes)
        if len(due) > params.per_tick:
            log.info(
                'grid: %d more post(s) wait for the next tick',
                len(due) - params.per_tick,
            )
        return [got for _, got in due]

    def _due(
        self,
        posts: list[userchat.Linked],
        feed: tuple[dict[str, str], ...],
    ) -> list[tuple[userchat.Linked, gridfill.Plan]]:
        """Plan each post; return those that gain a network.

        A catch-up post left as it is says why in the log: it was named
        on purpose, so a silent skip would hide a failure.
        """
        params = self.deps.params
        due = []
        for post in posts:
            got = gridfill.plan(to_post(post), feed, params.layout)
            if got.added:
                due.append((post, got))
            elif post.id in params.posts:
                log.info('grid: post %s left: %s', post.id, got.reason)
        return due

    async def _posts(self, now: float) -> list[userchat.Linked]:
        """Return the window's newest posts and the catch-up ones, once."""
        params = self.deps.params
        chat = self.deps.channel()
        since = now - params.window_days * DAY_SEC
        newest = await self.deps.account.linked(chat, params.page)
        found = {p.id: p for p in newest if p.date >= since}
        extra = [i for i in params.posts if i not in found]
        if extra:
            named = await self.deps.account.posts(chat, extra)
            if named is None:
                log.warning('grid: catch-up posts unreadable this tick')
            else:
                found.update(named)
        return [found[i] for i in sorted(found, reverse=True)]

    async def _apply(
        self, post: userchat.Linked, plan: gridfill.Plan, writes: int
    ) -> None:
        """Log the plan; in ``on`` mode journal the post and edit it."""
        added = ', '.join(plan.added)
        if self.deps.params.mode != 'on':
            log.info('grid dry: post %s + %s', plan.post_id, added)
            return
        if writes:
            await self.deps.sleep(self.deps.params.pause_sec)
        self._journal('post', userchat.dump(post))
        edit = userchat.Relinked(
            plan.text, plan.spans, emoji=plan.emoji, clones=plan.clones
        )
        chat = self.deps.channel()
        landed = await self.deps.account.relink(chat, post, edit)
        status = tgedit.APPLIED if landed else tgedit.ERROR
        self._journal(
            'outcome',
            {
                'post': plan.post_id,
                'added': list(plan.added),
                'status': status,
            },
        )
        log.info('grid: post %s + %s -> %s', plan.post_id, added, status)

    def _journal(self, what: str, body: dict[str, object]) -> None:
        """Append one row: a before-image (before the write) or an outcome."""
        row = {'at': time.time(), 'what': what, **body}
        line = json.dumps(row, ensure_ascii=False) + '\n'
        with self.deps.journal.open('a', encoding='utf-8') as out:
            out.write(line)
