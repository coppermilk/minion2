# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Render the /status report -- a read-only view over the whole bot.

One section builder per service, each reading that service off the bot and
writing nothing back. The report is the operator's only window into six
engines at once, so its exact text is pinned by tests/test_status.py.
"""

from __future__ import annotations

import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import TYPE_CHECKING

from minions.userbot.core import humanize
from minions.userbot.core.render import emoji_markup
from minions.userbot.core.render import trim
from minions.userbot.core.runtime import fmt_eta
from minions.userbot.glue.commands import SERVICE_ACTIONS
from minions.userbot.glue.commands import SERVICE_NAMES

# How many queued rows (reactions, story views) to list before summing the
# rest as "... (+N more)".
PENDING_ROWS = 12
# How many of the warmest story peers to list in the attachment readout.
STATUS_WARM_PEERS = 3

if TYPE_CHECKING:
    from minions.userbot.core import relationship
    from minions.userbot.engines import reactions
    from minions.userbot.main import Userbot


def _capped(rows: list[str], cap: int = PENDING_ROWS) -> list[str]:
    """Return at most ``cap`` rows, summing the remainder as a tail line."""
    if len(rows) <= cap:
        return rows
    return [*rows[:cap], f'    ... (+{len(rows) - cap} more)']


def _clock_eta(at: float, tz_offset: float) -> str:
    """Return "HH:MM (in 8m 12s)" for ``at``, read in the persona's zone."""
    zone = timezone(timedelta(hours=tz_offset))
    clock = datetime.fromtimestamp(at, tz=zone).strftime('%H:%M')
    eta = at - time.time()
    return f'{clock} (in {"now" if eta <= 0 else fmt_eta(eta)})'


def _pool_markup(pool: tuple[reactions.ReactionEmoji, ...]) -> str:
    """Render a whole emoji pool as premium markup (a preview strip)."""
    return ''.join(emoji_markup(c.emoji_id, c.fallback) for c in pool) or '-'


class StatusReport:
    """Render the /status report from a live Userbot -- read-only.

    Holds the bot rather than sharing its ``self``: every line below can be
    traced to ``self.bot.<service>``, and nothing here can write to it.
    """

    def __init__(self, bot: Userbot) -> None:
        """Bind the bot whose state this report renders."""
        self.bot = bot

    def _glyph(self, key: str, fallback: str = '') -> str:
        """Return a /status glyph from the JSON, or the fallback."""
        return self.bot.consts.status.get(key) or fallback

    def _dot(self, *, on: bool) -> str:
        """Return the green/red status dot."""
        return self._glyph('on', '[on]') if on else self._glyph('off', '[off]')

    def bullet(self) -> str:
        """Return the bullet glyph leading sub-lines and joining headers."""
        return self._glyph('bullet', '-')

    def arrow(self) -> str:
        """Return the arrow glyph ('next ...' / 'posting ...')."""
        return self._glyph('arrow', '->')

    def _header(self, key: str, label: str, *tail: str) -> str:
        """'icon label [ . tail . tail ]', skipping any blank piece."""
        title = ' '.join(p for p in (self._glyph(key), label) if p)
        sep = f' {self.bullet()} '
        return sep.join([title, *(t for t in tail if t)])

    def text(self, labels: dict[int, str]) -> str:
        """Return the /status text: header, routing, videos, engines."""
        flag = 'TEST' if self.bot.mode == 'test' else 'LIVE'
        parts = [
            self._header('title', 'Userbot', f'{self._dot(on=True)} {flag}'),
            '',
            *self._routing_lines(labels),
            '',
            *self._videos_lines(),
            '',
            *self._react_status_lines(labels),
            '',
            *self._greeter_lines(),
            '',
            self._users_line(),
            *self._stories_lines(labels),
            '',
            *self._services_lines(),
        ]
        if self.bot.consts.status_help:
            legend = ' '.join(
                p
                for p in (self._glyph('legend'), self.bot.consts.status_help)
                if p
            )
            parts += ['', legend]
        return '\n'.join(parts)

    def _greeter_lines(self) -> list[str]:
        """Greeter section: on/off, DMs today, admin-log cursor, next check."""
        gp = self.bot.greeter.params
        gs = self.bot.greeter.state
        state = 'on' if gp.enabled else 'off'
        head = self._header(
            'greeter',
            'Greeter',
            f'{self._dot(on=gp.enabled)} {state}',
            f'DMs {gs.dm_today}/{gp.max_dm_per_day}',
            f'last event {gs.last_event_id}',
        )
        if not gp.enabled:
            return [head]
        return [head, self._greeter_schedule_line()]

    def _greeter_schedule_line(self) -> str:
        """Return the greeter's awake/asleep state and its next action time.

        When asleep (outside the persona wake window) DMs are held, so this
        says so, when it wakes, and how many events are queued -- answering
        "is anything queued?" instead of a silent, idle-looking greeter.
        """
        gp = self.bot.greeter.params
        b = self.bullet()
        now = time.time()
        if not self.bot.greeter.awake(now):
            window = f'{gp.wake_start_hour:g}-{gp.wake_end_hour:g}h'
            return (
                f'{b} asleep (wake {window}) {self.arrow()} '
                f'wakes {self._greeter_wake_eta(now)} '
                f'{b} {self.bot.greeter.deferred} queued'
            )
        period = int(gp.poll_sec)
        nxt = self.bot.greeter.next_sync
        if nxt <= 0:
            return f'{b} check {period}s {b} next: first run'
        return (
            f'{b} check {period}s {self.arrow()} '
            f'next {_clock_eta(nxt, gp.tz_offset_hours)}'
        )

    def _greeter_wake_eta(self, now: float) -> str:
        """Return 'HH:MM (in Xd Yh)' for the greeter's next wake-up."""
        gp = self.bot.greeter.params
        local = humanize.local(now, gp.tz_offset_hours)
        wake = local.replace(
            hour=int(gp.wake_start_hour), minute=0, second=0, microsecond=0
        )
        if wake <= local:
            wake += timedelta(days=1)
        return _clock_eta(wake.timestamp(), gp.tz_offset_hours)

    def _routing_lines(self, labels: dict[int, str]) -> list[str]:
        """Source, the live targets, and where posts go NOW (test vs live)."""
        source = labels.get(
            self.bot.config.source, str(self.bot.config.source)
        )
        targets = ', '.join(
            labels.get(t, str(t)) for t in self.bot.config.targets
        )
        dest = ', '.join(
            labels.get(t, str(t)) for t in self.bot.live_targets()
        )
        b = self.bullet()
        return [
            self._header('routing', 'Routing'),
            f'{b} source: {source}',
            f'{b} target: {targets}',
            f'{b} posting {self.arrow()} {dest}',
        ]

    def _guard_desc(self) -> str:
        """One-line summary of the active re-post guard windows.

        Shows both windows so the operator can confirm dedup is armed: the
        time window (e.g. '7d') and the count window (e.g. 'last 5'). Each
        reads 'off' when its knob is 0; 'off' overall when both are.
        """
        secs = self.bot.config.repost_guard
        count = self.bot.config.repost_guard_count
        time_part = fmt_eta(secs) if secs > 0 else 'off'
        count_part = f'last {count}' if count > 0 else 'off'
        if secs <= 0 and count <= 0:
            return 'off'
        return f'{time_part}/{count_part}'

    def _videos_lines(self) -> list[str]:
        """Videos: counts on the header, then pending + recent posts."""
        b = self.bullet()
        window = fmt_eta(self.bot.config.timeout)
        poster = self.bot.aggregator
        lines = [
            self._header(
                'videos',
                'Videos',
                f'pending {len(poster.groups)} (timeout {window})',
                f'posted {len(poster.posted)}',
                f'rejected {len(poster.rejected)}',
                f'guard {self._guard_desc()}',
            )
        ]
        for group in poster.groups:
            have = ', '.join(sorted(group.items)) or '-'
            missing = (
                ', '.join(
                    p
                    for p in self.bot.config.platforms
                    if p not in group.items
                )
                or 'complete'
            )
            left = self.bot.config.timeout - (time.time() - group.created_at)
            lines.append(
                f'{b} "{trim(group.title)}" have [{have}] wait [{missing}]'
                f' {self.arrow()} ~{fmt_eta(left)}'
            )
        lines.extend(
            f'{b} "{trim(post.title)}" {b} {post.at[:10]}'
            f' {b} {len(post.links)} links'
            for post in poster.posted[-5:]
        )
        return lines

    def _react_status_lines(self, labels: dict[int, str]) -> list[str]:
        """Return the reaction engine's live state (empty when off)."""
        brain = self.bot.reactions
        b = self.bullet()
        enabled = brain.params.enabled
        state = 'on' if enabled else 'off'
        window = f'{brain.params.active_start:g}-{brain.params.active_end:g}h'
        alive = brain.state.alive
        top = sorted(alive, key=lambda h: alive[h], reverse=True)[:6]
        learned = ', '.join(f'{h}h' for h in top) or '(learning)'
        likes = _pool_markup(brain.params.like_pool)
        pool = _pool_markup(brain.params.pool)
        return [
            self._header(
                'reactions',
                'Reactions',
                f'{self._dot(on=enabled)} {state}',
                f'{len(brain.params.pool)} reactions / '
                f'{len(brain.params.like_pool)} likes',
            ),
            f'{b} likes {self.arrow()} {likes}',
            f'{b} reactions {self.arrow()} {pool}',
            (
                f'{b} mood {brain.state.mood:.2f} {b} answered '
                f'{brain.answered()} {b} pending '
                f'{len(brain.state.pending)}'
            ),
            f'{b} window {window} (prior) {b} learned {learned}',
            self._react_rescan_line(),
            *self._react_attach_lines(),
            *self._last_posts_lines(labels),
            *self._pending_react_lines(),
            f'{b} /reactnow {b} /requeue',
        ]

    def _warmth_lines(
        self, warm: list[relationship.Warmth], noun: str
    ) -> list[str]:
        """Return one Berlyne ledger's readout: aggregate, then recent peers.

        Exposure p = taken/offered (steered to the Wundt peak ~0.67),
        reciprocity r = recip/taken (steered to 0.20); A~ is the partial
        index exposure(p)*recip(r). The comment likes and the story views
        keep the same ledger shape, so they read out through one function --
        ``noun`` is all that differs. Labels are the ledger's cached @names.
        """
        if not warm:
            return []
        b = self.bullet()
        mean_p = sum(w.p for w in warm) / len(warm)
        mean_r = sum(w.r for w in warm) / len(warm)
        return [
            (
                f'{b} attach {len(warm)} {noun} {self.arrow()} '
                f'p~{mean_p:.2f} r~{mean_r:.2f}'
            ),
            *(
                f'    {w.label}  A {w.index:.2f} {b} p {w.p:.2f} r {w.r:.2f}'
                for w in warm[:STATUS_WARM_PEERS]
            ),
        ]

    def _react_attach_lines(self) -> list[str]:
        """Return today's like/sticker budget, then the commenter readout."""
        brain = self.bot.reactions
        if not brain.params.attach_enabled:
            return []
        now = time.time()
        b = self.bullet()
        today = (
            f'{b} today likes {brain.likes_today(now)}/'
            f'{brain.params.like_max_per_day} {b} stickers '
            f'{brain.stickers_today(now)}/{brain.params.sticker_max_per_day}'
        )
        return [today, *self._warmth_lines(brain.warmth(), 'commenters')]

    def _react_rescan_line(self) -> str:
        """Return the auto-rescan period and the countdown to the next one."""
        b = self.bullet()
        period = int(self.bot.comment_watch.deps.rescan_sec)
        if period <= 0:
            return f'{b} rescan: off (use /requeue)'
        nxt = self.bot.comment_watch.next_rescan
        if nxt <= 0:
            return f'{b} rescan {period}s {b} next: first run'
        tz = self.bot.reactions.params.tz_offset_hours
        return (
            f'{b} rescan {period}s {self.arrow()} next {_clock_eta(nxt, tz)}'
        )

    def _pending_react_lines(self) -> list[str]:
        """Return queued reactions: which lands on which comment, when."""
        rows = self.bot.comment_watch.queued_rows()
        return [f'{self.bullet()} queued:', *rows] if rows else []

    def _last_posts_lines(self, labels: dict[int, str]) -> list[str]:
        """Return the watched comment threads, grouped one line per chat."""
        posts = self.bot.reactions.posts
        if not posts:
            return []
        by_chat: dict[int, list[int]] = {}
        for chat, mid in posts:
            by_chat.setdefault(chat, []).append(mid)
        lines = [f'{self.bullet()} watching {len(posts)} posts:']
        lines.extend(
            f'    {labels.get(chat, str(chat))}: '
            f'{", ".join(str(m) for m in mids)}'
            for chat, mids in by_chat.items()
        )
        return lines

    def _services_lines(self) -> list[str]:
        """Return the service control table: each mode + its tap commands.

        Underscore commands (``/reactions_test`` ...) so Telegram renders each
        as a
        single tappable command; every service is off/test/live on its own.
        """
        lines = [self._header('services', 'Services')]
        for name in SERVICE_NAMES:
            mode = self.bot.modes.mode_of(name)
            dot = self._dot(on=mode != 'off')
            cmds = '  '.join(f'/{name}_{a}' for a in SERVICE_ACTIONS)
            lines.append(f'{self.bullet()} {dot} {name}: {mode.upper()}')
            lines.append(f'   {cmds}')
        return lines

    def _stories_lines(self, labels: dict[int, str]) -> list[str]:
        """Return the story-viewer section: header plus the view queue."""
        if not self.bot.stories.params.enabled:
            off = f'{self._dot(on=False)} off'
            return [self._header('stories', 'Stories', off)]
        return [
            self._stories_line(),
            *self._stories_queue_lines(labels),
            *self._warmth_lines(self.bot.stories.warmth(), 'peers'),
        ]

    def _stories_line(self) -> str:
        """Return the story-viewer header: on, count, next view, next poll."""
        now = time.time()
        tz = self.bot.stories.params.tz_offset_hours
        today = self.bot.stories.views_today(now, tz)
        reacted = self.bot.stories.reacts_today(now, tz)
        cap = self.bot.stories.params.react_max_per_day
        parts = [
            f'{self._dot(on=True)} on',
            f'{today} today',
            f'{reacted}/{cap} reacted',
            f'{len(self.bot.story_watch.pending)} queued',
        ]
        whens = [v.when for v in self.bot.story_watch.pending]
        if whens:
            eta = min(whens) - now
            when = 'now' if eta <= 0 else f'in {fmt_eta(eta)}'
            parts.append(f'next view {self.arrow()} {when}')
        else:
            # Empty queue: say WHY (asleep, cooldown, silent day) so it is not
            # a mystery -- the same reason the poll logs.
            reason = self.bot.stories.blocked_reason(now)
            if reason:
                parts.append(f'idle ({reason})')
        nxt = self.bot.story_watch.next_poll
        poll_eta = nxt - now if nxt else 0.0
        if poll_eta > 0:
            parts.append(f'next poll {self.arrow()} in {fmt_eta(poll_eta)}')
        return self._header('stories', 'Stories', *parts)

    def _stories_queue_lines(self, labels: dict[int, str]) -> list[str]:
        """Return the queued story views: whose, how many, and the ETA."""
        if not self.bot.story_watch.pending:
            return []
        now = time.time()
        b = self.bullet()
        rows = []
        for view in sorted(self.bot.story_watch.pending, key=lambda v: v.when):
            who = labels.get(view.peer_id, str(view.peer_id))
            eta = view.when - now
            when = 'due now' if eta <= 0 else f'in ~{fmt_eta(eta)}'
            rows.append(
                f'    {who} {b} {len(view.story_ids)} story(s) {b} {when}'
            )
        return [f'{b} queued:', *_capped(rows)]

    def _users_line(self) -> str:
        """Return a one-line users summary for /status ('off' if disabled)."""
        if not self.bot.audience.deps.enabled:
            return self._header(
                'users', 'Users DB', f'{self._dot(on=False)} off'
            )
        s = self.bot.audience.deps.store.summary()
        return self._header(
            'users',
            'Users DB',
            f'{self._dot(on=True)} on',
            f'{s["total"]} users',
            f'{s["subscribed"]} subscribed',
            f'{s["messages"]} msgs',
        )
