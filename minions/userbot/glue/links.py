# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Keep the channel's links short: the Telethon side of ``core/relink``.

A background worker. Each tick, inside the persona's waking hours, it reads
the newest page of the channel (new and recently edited posts) and one page
further down the history, and plans every post with ``core/relink``:

* ``off`` -- does nothing;
* ``dry`` (the default) -- edits nothing and sends the operator the lines
  ``post N: old -> new`` once per post, plus the links it leaves alone;
* ``on`` -- edits the posts, at most ``cap_per_day`` edits a local day,
  ``pause_sec`` apart, and stops the pass when the write lane is strained
  (a FloodWait widened it) -- the next tick resumes where this one stopped.

Only the operator's command in the source chat changes the mode
(``/links_off``, ``/links_dry``, ``/links_on``; ``/links`` reports). A post
with nothing left to change yields an empty plan, so a second pass edits
nothing; the only state kept is the mode, the history cursor and today's
edit count (``links.json``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from typing import TYPE_CHECKING

from minion_core.adapters import userchat
from minion_core.kernel import atomic_write
from minions.userbot.core import codec
from minions.userbot.core import relink

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from collections.abc import Mapping
    from pathlib import Path

log = logging.getLogger('userbot')

STATE_FILE = 'links.json'
DONE = -1
"""The history cursor once the walk reached the channel's first post."""
CHUNK = 3500
"""Characters per operator message (Telegram's limit is 4096)."""
COMMANDS = ('/links', '/links_off', '/links_dry', '/links_on')


@dataclass(frozen=True)
class LinkParams:
    """The worker's knobs, from ``engines.links`` and ``persona``."""

    domain: str = ''
    cap_per_day: int = 20
    delay_sec: float = 1800.0
    tick_sec: float = 900.0
    page: int = 20
    pause_sec: float = 60.0
    clock: relink.Clock = field(default_factory=relink.Clock)


def load_link_params(settings: Mapping[str, object]) -> LinkParams:
    """Read ``engines.links``; the domain defaults to the post's own."""
    cfg = codec.engine(settings, 'links')
    persona = codec.section(settings, 'persona')
    post = codec.section(settings, 'post')
    domain = (
        codec.text(cfg.get('domain'), '').strip()
        or codec.text(post.get('short_link_domain'), '').strip()
    )
    return LinkParams(
        domain=domain,
        cap_per_day=int(codec.num(cfg.get('cap_per_day'), 20)),
        delay_sec=codec.num(cfg.get('delay_sec'), 1800.0),
        tick_sec=codec.num(cfg.get('tick_sec'), 900.0),
        page=max(1, int(codec.num(cfg.get('page'), 20))),
        pause_sec=codec.num(cfg.get('pause_sec'), 60.0),
        clock=relink.Clock(
            tz_offset_hours=codec.num(persona.get('tz_offset_hours'), 0.0),
            wake_start_hour=int(codec.num(persona.get('wake_start_hour'), 0)),
            wake_end_hour=int(codec.num(persona.get('wake_end_hour'), 24)),
        ),
    )


@dataclass(frozen=True)
class LinkDeps:
    """Everything the worker may reach; nothing else is in scope."""

    account: userchat.Account
    channel: Callable[[], int]  # the profile's channel (test or live)
    say: Callable[[str], Awaitable[None]]  # a line to the operator
    path: Path  # links.json
    params: LinkParams


@dataclass
class LinkState:
    """What survives a restart: the mode, the cursor, today's edits."""

    mode: str = relink.DEFAULT_MODE
    cursor: int = 0
    budget: relink.Budget = field(default_factory=relink.Budget)


def read_state(path: Path) -> LinkState:
    """Load ``links.json``; anything unreadable starts a fresh dry walk."""
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return LinkState()
    if not isinstance(raw, dict):
        return LinkState()
    mode = raw.get('mode')
    return LinkState(
        mode=mode if mode in relink.MODES else relink.DEFAULT_MODE,
        cursor=int(codec.num(raw.get('cursor'), 0)),
        budget=relink.Budget(
            codec.text(raw.get('day'), ''), int(codec.num(raw.get('used'), 0))
        ),
    )


def write_state(path: Path, state: LinkState) -> None:
    """Persist the state atomically."""
    body = {
        'mode': state.mode,
        'cursor': state.cursor,
        'day': state.budget.day,
        'used': state.budget.used,
    }
    atomic_write(path, json.dumps(body, indent=2).encode('utf-8'))


def chunks(lines: list[str], size: int = CHUNK) -> list[str]:
    """Pack report lines into messages no longer than ``size``."""
    out: list[str] = []
    current = ''
    for line in lines:
        piece = line[:size]
        if current and len(current) + 1 + len(piece) > size:
            out.append(current)
            current = ''
        current = f'{current}\n{piece}' if current else piece
    if current:
        out.append(current)
    return out


def to_post(linked: userchat.Linked) -> relink.Post:
    """Convert the adapter's post into the pure planner's."""
    links = tuple(
        relink.Link(one.kind, one.offset, one.length, one.url)
        for one in linked.links
    )
    return relink.Post(linked.text, linked.spans, links)


def to_edit(linked: userchat.Linked, plan: relink.Plan) -> userchat.Relinked:
    """Turn a plan into the adapter's edit: text, spans, hidden urls."""
    edit = relink.apply(to_post(linked), plan.changes)
    urls = {
        linked.links[c.index].index: c.new
        for c in plan.changes
        if linked.links[c.index].kind == relink.HIDDEN
    }
    return userchat.Relinked(edit.text, edit.spans, urls)


@dataclass
class LinkKeeper:
    """Walk the channel and keep every video link short."""

    deps: LinkDeps
    state: LinkState = field(default_factory=LinkState)
    # Posts already shown in a dry report this run, so the newest page,
    # re-read every tick, is reported once. In memory: a restart repeats.
    reported: set[int] = field(default_factory=set)

    def load(self) -> None:
        """Read the persisted state."""
        self.state = read_state(self.deps.path)

    def save(self) -> None:
        """Write the state."""
        write_state(self.deps.path, self.state)

    async def loop(self) -> None:
        """Tick forever; one failed tick is logged and retried later."""
        while True:
            try:
                await self.tick(time.time())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('links: tick failed; will retry')
            await asyncio.sleep(self.deps.params.tick_sec)

    async def tick(self, now: float) -> int:
        """Run one pass; return how many posts it edited (dry: 0)."""
        params = self.deps.params
        if self.state.mode == 'off' or not params.domain:
            return 0
        if not params.clock.awake(now):
            return 0
        posts = await self._posts()
        due = [p for p in posts if relink.due(p.date, now, params.delay_sec)]
        if self.state.mode == 'dry':
            await self._report(due)
            self._advance(posts)
            return 0
        return await self._apply(due, posts, now)

    async def _posts(self) -> list[userchat.Linked]:
        """Read the newest page plus the next history page, newest first."""
        account, channel, page = (
            self.deps.account,
            self.deps.channel(),
            self.deps.params.page,
        )
        top = await account.linked(channel, page)
        if self.state.cursor in {0, DONE}:
            return top
        older = await account.linked(channel, page, self.state.cursor)
        seen = {p.id for p in top}
        return top + [p for p in older if p.id not in seen]

    def _advance(self, posts: list[userchat.Linked]) -> None:
        """Move the history cursor below the oldest post just read."""
        if self.state.cursor == DONE:
            return
        lowest = min((p.id for p in posts), default=0)
        if lowest <= 1 or (self.state.cursor and lowest >= self.state.cursor):
            self.state.cursor = DONE
        else:
            self.state.cursor = lowest
        self.save()

    def _plan(self, post: userchat.Linked) -> relink.Plan:
        """Plan one post against the configured domain."""
        return relink.plan(
            post.id, to_post(post).links, self.deps.params.domain
        )

    def _fresh(self, posts: list[userchat.Linked]) -> list[relink.Plan]:
        """Plan the posts not reported yet this run; mark them reported."""
        fresh = [p for p in posts if p.id not in self.reported]
        self.reported.update(p.id for p in fresh)
        plans = [self._plan(p) for p in fresh]
        return [one for one in plans if one.changes or one.others]

    async def _report(self, posts: list[userchat.Linked]) -> None:
        """Send the dry-run lines for posts not reported yet this run."""
        lines = relink.report(self._fresh(posts))
        if not lines:
            return
        for message in chunks(['links (dry): would change', *lines]):
            await self.deps.say(message)
        log.info('links: dry report, %d line(s)', len(lines))

    async def _apply(
        self,
        due: list[userchat.Linked],
        posts: list[userchat.Linked],
        now: float,
    ) -> int:
        """Edit due posts within today's budget; stop on a strained lane."""
        today = self.deps.params.clock.day(now)
        edited = 0
        for post in due:
            one = self._plan(post)
            if not one.changes:
                continue
            why = self._blocked(today)
            if why:
                return self._stop(post, edited, why)
            edited += await self._edit(post, one, today)
        self._advance(posts)
        return edited

    def _blocked(self, today: str) -> str:
        """Return why no edit may go out now, '' when one may."""
        if self.state.budget.left(today, self.deps.params.cap_per_day) <= 0:
            return 'daily cap'
        if self.deps.account.strained(userchat.WRITE):
            return 'flood wait'
        return ''

    async def _edit(
        self, post: userchat.Linked, one: relink.Plan, today: str
    ) -> int:
        """Edit one post, spend the budget, pause; return 1 when it landed."""
        landed = await self.deps.account.relink(
            self.deps.channel(), post, to_edit(post, one)
        )
        if landed:
            self.state.budget = self.state.budget.spend(today)
            self.save()
            log.info('links: post %d, %d link(s)', post.id, len(one.changes))
        await asyncio.sleep(self.deps.params.pause_sec)
        return int(landed)

    def _stop(self, post: userchat.Linked, edited: int, why: str) -> int:
        """End a pass early; the next one resumes at ``post``."""
        if self.state.cursor not in {0, DONE} and post.id < self.state.cursor:
            self.state.cursor = post.id + 1
        self.save()
        log.info('links: stopped (%s) after %d edit(s)', why, edited)
        return edited

    async def command(self, word: str) -> bool:
        """Handle an operator /links command; False when it is not one."""
        if word not in COMMANDS:
            return False
        if word != '/links':
            mode = word.removeprefix('/links_')
            if mode != self.state.mode:
                self.state = replace(self.state, mode=mode, cursor=0)
                self.reported.clear()
                self.save()
        await self.deps.say(self.text())
        return True

    def text(self) -> str:
        """Render the /links readout: mode, cursor, today's edits."""
        params = self.deps.params
        cursor = {0: 'from the top', DONE: 'history done'}.get(
            self.state.cursor, f'below post {self.state.cursor}'
        )
        today = params.clock.day(time.time())
        left = self.state.budget.left(today, params.cap_per_day)
        return (
            f'links: {self.state.mode} . {cursor} . '
            f'{params.cap_per_day - left}/{params.cap_per_day} edits today'
            f' . domain {params.domain or "(none: idle)"}'
        )
