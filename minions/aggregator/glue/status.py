# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Render the /status report -- a read-only view over the aggregator.

Extracted from ``main``: the section builders and small display helpers that
turn the live aggregator state into the /status text. ``_StatusMixin`` is
mixed into ``Aggregator`` (its method bodies are unchanged), so they keep
reading ``self`` state; it inherits ``AggregatorProtocol`` (base.py) so the
type checker knows what that state is.
"""

from __future__ import annotations

import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import TYPE_CHECKING

from minions.aggregator.core.base import AggregatorProtocol
from minions.aggregator.core.render import _emoji_markup
from minions.aggregator.core.runtime import _fmt_eta
from minions.aggregator.glue.commands import SERVICE_ACTIONS
from minions.aggregator.glue.commands import SERVICE_NAMES

# How many pending cats to list individually in /status (the rest are summed).
STATUS_PENDING_CATS = 12

if TYPE_CHECKING:
    from minions.aggregator.engines import cats

def _trim(title: str, width: int = 40) -> str:
    """Return a one-line, length-capped title for the /status report."""
    flat = ' '.join(title.split())
    return flat if len(flat) <= width else flat[: width - 1] + '~'


_EMOJI_ROW_LEN = 2
"""A persisted emoji row is an [id, fallback] pair."""


def _pending_markup(entry: dict[str, object]) -> str:
    """Render a pending entry's chosen cat(s) as premium markup, or '?'.

    Reactions scheduled before the emoji were stored show '?' -- a /requeue
    does not re-pick them (the choice is made at schedule time), it only
    re-times what is already there.
    """
    raw = entry.get('emojis')
    rows = raw if isinstance(raw, list) else []
    markup = ''.join(
        _emoji_markup(str(row[0]), str(row[1]))
        for row in rows
        if len(row) == _EMOJI_ROW_LEN
    )
    return markup or '?'


def _pool_markup(pool: tuple[cats.CatEmoji, ...]) -> str:
    """Render a whole emoji pool as premium markup (a preview strip)."""
    return ''.join(_emoji_markup(c.emoji_id, c.fallback) for c in pool) or '-'


def _user_label(row: dict[str, object]) -> str:
    """Return a readable handle for a users-DB row: @username/name/id."""
    username = row.get('username')
    if username:
        return f'@{username}'
    name = row.get('first_name')
    if name:
        return str(name)
    return f'id {row.get("user_id", "?")}'


class _StatusMixin(AggregatorProtocol):
    """The /status renderer, mixed into Aggregator (reads its state)."""

    def _ic(self, key: str, fallback: str = '') -> str:
        """Return a /status glyph from the JSON, or the fallback."""
        return self.consts.status.get(key) or fallback

    def _dot(self, *, on: bool) -> str:
        """Return the green/red status dot."""
        return self._ic('on', '[on]') if on else self._ic('off', '[off]')

    def _bul(self) -> str:
        """Return the bullet glyph leading sub-lines and joining headers."""
        return self._ic('bullet', '-')

    def _arr(self) -> str:
        """Return the arrow glyph ('next ...' / 'posting ...')."""
        return self._ic('arrow', '->')

    def _head(self, key: str, label: str, *tail: str) -> str:
        """'icon label [ . tail . tail ]', skipping any blank piece."""
        title = ' '.join(p for p in (self._ic(key), label) if p)
        sep = f' {self._bul()} '
        return sep.join([title, *(t for t in tail if t)])

    def _status_text(self, labels: dict[int, str]) -> str:
        """Return status: header, routing, videos, cats, greeter, users."""
        flag = 'TEST' if self.mode == 'test' else 'LIVE'
        parts = [
            self._head('title', 'Aggregator', f'{self._dot(on=True)} {flag}'),
            '',
            *self._routing_lines(labels),
            '',
            *self._videos_lines(),
            '',
            *self._cat_status_lines(labels),
            '',
            *self._greeter_lines(),
            '',
            self._users_line(),
            *self._stories_lines(labels),
            '',
            *self._services_lines(),
        ]
        if self.consts.status_help:
            legend = ' '.join(
                p for p in (self._ic('legend'), self.consts.status_help) if p
            )
            parts += ['', legend]
        return '\n'.join(parts)

    def _greeter_lines(self) -> list[str]:
        """Greeter section: on/off, DMs today, admin-log cursor, next check."""
        gp = self.greeter.params
        gs = self.greeter.state
        state = 'on' if gp.enabled else 'off'
        head = self._head(
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
        gp = self.greeter.params
        b = self._bul()
        now = time.time()
        if not self.greeter.awake(now):
            window = f'{gp.wake_start_hour:g}-{gp.wake_end_hour:g}h'
            return (
                f'{b} asleep (wake {window}) {self._arr()} '
                f'wakes {self._greeter_wake_eta(now)} '
                f'{b} {self.greeter.deferred} queued'
            )
        period = int(gp.poll_sec)
        nxt = self.greeter.next_sync
        if nxt <= 0:
            return f'{b} check {period}s {b} next: first run'
        tz = timezone(timedelta(hours=gp.tz_offset_hours))
        clock = datetime.fromtimestamp(nxt, tz=tz).strftime('%H:%M')
        eta = nxt - now
        when = 'now' if eta <= 0 else _fmt_eta(eta)
        return (
            f'{b} check {period}s {self._arr()} next {clock} (in {when})'
        )

    def _greeter_wake_eta(self, now: float) -> str:
        """Return 'HH:MM (in Xd Yh)' for the greeter's next wake-up."""
        gp = self.greeter.params
        tz = timezone(timedelta(hours=gp.tz_offset_hours))
        local = datetime.fromtimestamp(now, tz=tz)
        wake = local.replace(
            hour=int(gp.wake_start_hour), minute=0, second=0, microsecond=0
        )
        if wake <= local:
            wake += timedelta(days=1)
        eta = _fmt_eta(wake.timestamp() - now)
        return f'{wake.strftime("%H:%M")} (in {eta})'

    def _routing_lines(self, labels: dict[int, str]) -> list[str]:
        """Source, the live targets, and where posts go NOW (test vs live)."""
        source = labels.get(self.config.source, str(self.config.source))
        targets = ', '.join(labels.get(t, str(t)) for t in self.config.targets)
        dest = ', '.join(labels.get(t, str(t)) for t in self.live_targets())
        b = self._bul()
        return [
            self._head('routing', 'Routing'),
            f'{b} source: {source}',
            f'{b} target: {targets}',
            f'{b} posting {self._arr()} {dest}',
        ]

    def _guard_desc(self) -> str:
        """One-line summary of the active re-post guard windows.

        Shows both windows so the operator can confirm dedup is armed: the
        time window (e.g. '7d') and the count window (e.g. 'last 5'). Each
        reads 'off' when its knob is 0; 'off' overall when both are.
        """
        secs = self.config.repost_guard
        count = self.config.repost_guard_count
        time_part = _fmt_eta(secs) if secs > 0 else 'off'
        count_part = f'last {count}' if count > 0 else 'off'
        if secs <= 0 and count <= 0:
            return 'off'
        return f'{time_part}/{count_part}'

    def _videos_lines(self) -> list[str]:
        """Videos: counts on the header, then pending + recent posts."""
        b = self._bul()
        window = _fmt_eta(self.config.timeout)
        lines = [
            self._head(
                'videos',
                'Videos',
                f'pending {len(self.groups)} (timeout {window})',
                f'posted {len(self.posted)}',
                f'rejected {len(self.rejected)}',
                f'guard {self._guard_desc()}',
            )
        ]
        for group in self.groups:
            have = ', '.join(sorted(group.items)) or '-'
            missing = (
                ', '.join(
                    p for p in self.config.platforms if p not in group.items
                )
                or 'complete'
            )
            left = self.config.timeout - (time.time() - group.created_at)
            lines.append(
                f'{b} "{_trim(group.title)}" have [{have}] wait [{missing}]'
                f' {self._arr()} ~{_fmt_eta(left)}'
            )
        lines.extend(
            f'{b} "{_trim(post.title)}" {b} {post.at[:10]}'
            f' {b} {len(post.links)} links'
            for post in self.posted[-5:]
        )
        return lines

    def _cat_status_lines(self, labels: dict[int, str]) -> list[str]:
        """Return the cat engine's live state (empty-ish when disabled)."""
        brain = self.cats
        b = self._bul()
        enabled = brain.params.enabled
        state = 'on' if enabled else 'off'
        window = f'{brain.params.active_start:g}-{brain.params.active_end:g}h'
        alive = brain.state.alive
        top = sorted(alive, key=lambda h: alive[h], reverse=True)[:6]
        learned = ', '.join(f'{h}h' for h in top) or '(learning)'
        likes = _pool_markup(brain.params.like_pool)
        pool = _pool_markup(brain.params.pool)
        return [
            self._head(
                'cats',
                'Cats',
                f'{self._dot(on=enabled)} {state}',
                f'{len(brain.params.pool)} cats / '
                f'{len(brain.params.like_pool)} likes',
            ),
            f'{b} likes {self._arr()} {likes}',
            f'{b} cats {self._arr()} {pool}',
            (
                f'{b} mood {brain.state.mood:.2f} {b} answered '
                f'{len(brain.state.catted)} {b} pending '
                f'{len(brain.state.pending)}'
            ),
            f'{b} window {window} (prior) {b} learned {learned}',
            self._cat_rescan_line(),
            *self._last_posts_lines(labels),
            *self._pending_cat_lines(),
            f'{b} /catnow {b} /requeue',
        ]

    def _cat_rescan_line(self) -> str:
        """Return the auto-rescan period and the countdown to the next one."""
        b = self._bul()
        period = int(self._rescan_sec)
        if period <= 0:
            return f'{b} rescan: off (use /requeue)'
        nxt = self._cat_next_rescan
        if nxt <= 0:
            return f'{b} rescan {period}s {b} next: first run'
        tz = timezone(timedelta(hours=self.cats.params.tz_offset_hours))
        clock = datetime.fromtimestamp(nxt, tz=tz).strftime('%H:%M')
        eta = nxt - time.time()
        when = 'now' if eta <= 0 else _fmt_eta(eta)
        return f'{b} rescan {period}s {self._arr()} next {clock} (in {when})'

    def _pending_cat_lines(self) -> list[str]:
        """Return the queued cats: which cat lands on which comment, when."""
        pending = self.cats.state.pending
        if not pending:
            return []
        now = time.time()
        lines = [f'{self._bul()} queued:']
        lines.extend(
            self._pending_cat_line(entry, now)
            for entry in pending[:STATUS_PENDING_CATS]
        )
        extra = len(pending) - STATUS_PENDING_CATS
        if extra > 0:
            lines.append(f'    ... (+{extra} more)')
        return lines

    def _pending_cat_line(self, entry: dict[str, object], now: float) -> str:
        """One queued line: '<cat> <verb> -> <comment> . post N . <eta>'."""
        b = self._bul()
        msg = int(entry.get('reply_to', 0))
        root = int(entry.get('root', msg))
        body = str(entry.get('text', ''))
        what = f'"{body}"' if body else f'comment {msg}'
        glyphs = _pending_markup(entry)
        verb = 'sticker' if entry.get('kind') == 'reply' else 'like'
        eta = float(entry.get('when', now)) - now
        when = 'due now' if eta <= 0 else f'in ~{_fmt_eta(eta)}'
        return (
            f'    {glyphs} {verb} {self._arr()} {what}'
            f' {b} post {root} {b} {when}'
        )

    def _last_posts_lines(self, labels: dict[int, str]) -> list[str]:
        """Return the watched comment threads, grouped one line per chat."""
        posts = self.cats.posts
        if not posts:
            return []
        by_chat: dict[int, list[int]] = {}
        for chat, mid in posts:
            by_chat.setdefault(chat, []).append(mid)
        lines = [f'{self._bul()} watching {len(posts)} posts:']
        lines.extend(
            f'    {labels.get(chat, str(chat))}: '
            f'{", ".join(str(m) for m in mids)}'
            for chat, mids in by_chat.items()
        )
        return lines

    def _services_lines(self) -> list[str]:
        """Return the service control table: each mode + its tap commands.

        Underscore commands (``/cats_test`` ...) so Telegram renders each as a
        single tappable command; every service is off/test/live on its own.
        """
        lines = [self._head('services', 'Services')]
        for name in SERVICE_NAMES:
            mode = self._modes.get(name, 'off')
            dot = self._dot(on=mode != 'off')
            cmds = '  '.join(f'/{name}_{a}' for a in SERVICE_ACTIONS)
            lines.append(f'{self._bul()} {dot} {name}: {mode.upper()}')
            lines.append(f'   {cmds}')
        return lines

    def _stories_lines(self, labels: dict[int, str]) -> list[str]:
        """Return the story-viewer section: header plus the view queue."""
        if not self.stories.params.enabled:
            return [
                self._head('stories', 'Stories', f'{self._dot(on=False)} off')
            ]
        return [self._stories_line(), *self._stories_queue_lines(labels)]

    def _stories_line(self) -> str:
        """Return the story-viewer header: on, count, next view, next poll."""
        now = time.time()
        tz = self.stories.params.tz_offset_hours
        today = self.stories.views_today(now, tz)
        parts = [
            f'{self._dot(on=True)} on',
            f'{today} today',
            f'{len(self._pending_views)} queued',
        ]
        whens = [v.when for v in self._pending_views]
        if whens:
            eta = min(whens) - now
            when = 'now' if eta <= 0 else f'in {_fmt_eta(eta)}'
            parts.append(f'next view {self._arr()} {when}')
        else:
            # Empty queue: say WHY (asleep, cooldown, silent day) so it is not
            # a mystery -- the same reason the poll logs.
            reason = self.stories.blocked_reason(now)
            if reason:
                parts.append(f'idle ({reason})')
        nxt = self._story_next_poll
        poll_eta = nxt - now if nxt else 0.0
        if poll_eta > 0:
            parts.append(f'next poll {self._arr()} in {_fmt_eta(poll_eta)}')
        return self._head('stories', 'Stories', *parts)

    def _stories_queue_lines(self, labels: dict[int, str]) -> list[str]:
        """Return the queued story views: whose, how many, and the ETA."""
        if not self._pending_views:
            return []
        now = time.time()
        b = self._bul()
        views = sorted(self._pending_views, key=lambda v: v.when)
        lines = [f'{b} queued:']
        for view in views[:STATUS_PENDING_CATS]:
            who = labels.get(view.peer_id, str(view.peer_id))
            eta = view.when - now
            when = 'due now' if eta <= 0 else f'in ~{_fmt_eta(eta)}'
            lines.append(
                f'    {who} {b} {len(view.story_ids)} story(s) {b} {when}'
            )
        extra = len(views) - STATUS_PENDING_CATS
        if extra > 0:
            lines.append(f'    ... (+{extra} more)')
        return lines

    def _users_line(self) -> str:
        """Return a one-line users summary for /status ('off' if disabled)."""
        if not self._users_enabled:
            return self._head(
                'users', 'Users DB', f'{self._dot(on=False)} off'
            )
        s = self.users.summary()
        return self._head(
            'users',
            'Users DB',
            f'{self._dot(on=True)} on',
            f'{s["total"]} users',
            f'{s["subscribed"]} subscribed',
            f'{s["messages"]} msgs',
        )
