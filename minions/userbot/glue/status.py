# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Render the /status report -- a read-only view over the whole bot.

One section builder per service, each reading that service off the bot and
writing nothing back. The report is the operator's only window into six
engines at once, so its exact text is pinned by tests/test_status.py.
"""

from __future__ import annotations

import time
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import TYPE_CHECKING

from minions.userbot.core import humanize
from minions.userbot.core import render
from minions.userbot.core.render import emoji_markup
from minions.userbot.core.render import trim
from minions.userbot.core.runtime import fmt_eta
from minions.userbot.core.state import ACTS
from minions.userbot.core.state import RUNGS
from minions.userbot.glue.commands import COMMAND_WHO
from minions.userbot.glue.commands import SERVICE_ACTIONS
from minions.userbot.glue.commands import SERVICE_NAMES

# How many queued rows (reactions, story views) to list before summing the
# rest as "... (+N more)".
PENDING_ROWS = 12
# How many of the warmest story peers to list in the attachment readout.
STATUS_WARM_PEERS = 3
TWO_WORDS = 2
"""A /who with no name is the usage line, not a lookup."""

UPTIME_ROWS = 6
"""How many of the busiest learned hours /status names."""

WHO_ROWS = 12
"""How many recent acts /who lists per service.

Enough to see a pattern, short enough to read on a phone. The
counters on the line above are over ALL of them, not just these.
"""

if TYPE_CHECKING:
    from minions.userbot.core import relationship
    from minions.userbot.core.models import Emoji
    from minions.userbot.core.state import Actor
    from minions.userbot.core.state import Database
    from minions.userbot.core.state import PeerRow
    from minions.userbot.engines import stories
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


def _waiting(row: stories.Seen) -> bool:
    """Whether a peer has something new that we are not opening."""
    return bool(row.unseen) and not row.viewing


def _glance_order(row: stories.Seen) -> tuple[int, int]:
    """Sort a glance: who we are opening first, then who is waiting."""
    rank = 1 if _waiting(row) else (0 if row.viewing else 2)
    return (rank, -row.unseen)


def _name(known: dict[int, Actor], peer_id: int) -> str:
    """Name a peer for a list of PEOPLE, where the raw id is noise."""
    found = known.get(peer_id)
    return (render.name(found) if found is not None else '') or str(peer_id)


def _tag(known: dict[int, Actor], peer_id: int) -> str:
    """Name a chat for the ROUTING lines, where the raw id is the point."""
    found = known.get(peer_id)
    return render.tagged(found) if found is not None else str(peer_id)


def _day(at: float) -> str:
    """Render a post's moment as a date, for the published list.

    The rendering side of the epoch: the file keeps one time format and
    ISO is made HERE, where somebody reads it, rather than stored so that
    the re-post guard has to parse a date to compare two moments.
    """
    return datetime.fromtimestamp(at, tz=UTC).strftime('%Y-%m-%d')


def _when(at: float) -> str:
    """Render one act's moment for /who: a date and a clock, in UTC."""
    return datetime.fromtimestamp(at, tz=UTC).strftime('%m-%d %H:%M')


def _drift(row: PeerRow, logged: dict[str, int]) -> str:
    """Say so when the counters and the log they summarise disagree.

    Silent when they agree, which is the normal case and needs no words --
    they are written in one transaction. Loud when they do not, because a
    counter that has drifted from its own history is the one thing /who
    exists to make visible: every percentage in /status is computed from
    the left-hand side of this comparison.
    """
    off = [
        f'{rung} {getattr(row, rung)}!={logged.get(rung, 0)}'
        for rung in RUNGS
        if getattr(row, rung) != logged.get(rung, 0)
    ]
    return f'  <- LOG DISAGREES: {", ".join(off)}' if off else ''


def _pool_markup(pool: tuple[Emoji, ...]) -> str:
    """Render a whole emoji pool as premium markup (a preview strip)."""
    return ''.join(emoji_markup(c.id, c.fallback) for c in pool) or '-'


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

    def text(self, known: dict[int, Actor]) -> str:
        """Return the /status text: header, routing, videos, engines."""
        flag = 'TEST' if self.bot.mode == 'test' else 'LIVE'
        parts = [
            self._header('title', 'Userbot', f'{self._dot(on=True)} {flag}'),
            '',
            *self._routing_lines(known),
            '',
            *self._videos_lines(),
            '',
            *self._react_status_lines(known),
            '',
            *self._greeter_lines(),
            '',
            self._users_line(),
            *self._stories_lines(known),
            '',
            *self._schedule_lines(),
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

    def who(self, words: list[str]) -> str:
        """Return one person's relationship history (the /who command).

        /status shows running totals; this shows what they are totals OF.
        The two are written in one transaction, so a percentage that looks
        wrong is settled by reading down this list rather than by guessing
        -- which is what asking it used to take.
        """
        if len(words) < TWO_WORDS:
            return f'{COMMAND_WHO} @name  (or an id)'
        peer_id = self._find(words[1].strip())
        if peer_id is None:
            return f'{words[1]}: never seen'
        return '\n'.join(self._who_lines(peer_id))

    def _db(self) -> Database:
        """Return the database /who reads: the story engine's profile.

        Identity and the contact log live in the FILE, not in one service's
        view of it, and the two engines that keep a ledger share one when
        their modes agree -- which they do unless somebody is sandboxing.
        """
        return self.bot.database('stories')

    def _find(self, wanted: str) -> int | None:
        """Resolve '@name' or a raw id to a peer we actually know."""
        if wanted.lstrip('-').isdigit():
            return int(wanted)
        row = (
            self._db()
            .conn.execute(
                'SELECT peer_id FROM actors WHERE username = ?',
                (wanted.lstrip('@'),),
            )
            .fetchone()
        )
        return int(row['peer_id']) if row is not None else None

    def _arc_line(self, peer_id: int, b: str) -> list[str]:
        """Say where this person is on their curve; nothing when it is off.

        Worth one line at the top of /who because it is the reason a
        percentage looks the way it does: somebody eleven days into a cold
        shoulder is SUPPOSED to read as neglected, and without this the
        readout below is a mystery to be re-derived from dates every time.
        """
        brain = self.bot.stories
        control = brain._control()  # noqa: SLF001 -- the arc is its own config
        if not control.arc.enabled:
            return []
        since = brain.store.met(peer_id)
        now = brain.clock()
        leg = control.arc.leg(since, now, peer_id)
        met = _when(since) if since > 0 else 'just now'
        return [
            f'{b} {leg.name}, round {control.arc.rounds(since, now)} '
            f'{b} met {met} {b} '
            f'aiming {control.take_target(leg):.0%} seen, '
            f'{control.recip_goal(leg):.0%} back'
        ]

    def _who_lines(self, peer_id: int) -> list[str]:
        """Return the header, the per-service totals, and the recent acts.

        Each service counts in its OWN words (``state.ACTS``), so the totals
        line reads the way the acts under it do -- ``6 seen . 2 like`` above
        a list of seens and likes, rather than a second vocabulary the reader
        has to translate.
        """
        db, b = self._db(), self.bullet()
        actor = db.actor(peer_id)
        lines = [f'{render.tagged(actor)}', *self._arc_line(peer_id, b)]
        for service, ladder in ACTS.items():
            store = db.store(service)
            row = store.peer(peer_id)
            if row.offered <= 0:
                continue
            _, took, back = ladder
            lines.append(
                f'{b} {service}: {row.offered} offered '
                f'{b} {row.taken} {took} {b} {row.recip} {back}'
                f'{_drift(row, store.tally(peer_id))}'
            )
            lines += [
                f'    {_when(c.at)} {c.act}'
                + (f' #{c.subject}' if c.subject else '')
                for c in store.history(peer_id, WHO_ROWS)
            ]
        return lines if len(lines) > 1 else [*lines, f'{b} nothing yet']

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
        return [head, *self._greeter_sleep_lines()]

    def _greeter_sleep_lines(self) -> list[str]:
        """Say so when the greeter is asleep, and what is waiting on it.

        Outside the persona's wake window DMs are held, so this names the
        window, when it opens, and how many events are queued -- without
        it a held greeter and a broken one look the same. The awake case
        needs no line here: its countdown lives in Schedule.
        """
        gp = self.bot.greeter.params
        now = time.time()
        if self.bot.greeter.awake(now):
            return []
        window = f'{gp.wake_start_hour:g}-{gp.wake_end_hour:g}h'
        b = self.bullet()
        return [
            (
                f'{b} asleep (wake {window}) {self.arrow()} '
                f'wakes {self._greeter_wake_eta(now)} '
                f'{b} {self.bot.greeter.deferred} queued'
            )
        ]

    def _greeter_wake_eta(self, now: float) -> str:
        """Return 'HH:MM (in 14h 30m)' for the greeter's next wake-up.

        Hours are the coarsest unit ``fmt_eta`` prints, and the next wake is
        under a day away by construction, so there is never a days field.
        """
        gp = self.bot.greeter.params
        local = humanize.local(now, gp.tz_offset_hours)
        wake = local.replace(
            hour=int(gp.wake_start_hour), minute=0, second=0, microsecond=0
        )
        if wake <= local:
            wake += timedelta(days=1)
        return _clock_eta(wake.timestamp(), gp.tz_offset_hours)

    def _routing_lines(self, known: dict[int, Actor]) -> list[str]:
        """Source, the live targets, and where posts go NOW (test vs live)."""
        source = _tag(known, self.bot.config.source)
        targets = ', '.join(_tag(known, t) for t in self.bot.config.targets)
        dest = ', '.join(_tag(known, t) for t in self.bot.live_targets())
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
            f'{b} "{trim(post.title)}" {b} {_day(post.at)}'
            f' {b} {len(post.links)} links'
            for post in poster.posted[-5:]
        )
        return lines

    def _react_status_lines(self, known: dict[int, Actor]) -> list[str]:
        """Return the reaction engine's live state (empty when off)."""
        brain = self.bot.reactions
        b = self.bullet()
        enabled = brain.params.enabled
        state = 'on' if enabled else 'off'
        window = f'{brain.params.active_start:g}-{brain.params.active_end:g}h'
        alive = brain.learned_hours()
        top = sorted(alive, key=lambda h: alive[h], reverse=True)[:UPTIME_ROWS]
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
            *([line] if (line := self._react_rescan_line()) else []),
            *self._react_attach_lines(known),
            *self._last_posts_lines(known),
            *self._pending_react_lines(),
            f'{b} /reactnow {b} /requeue',
        ]

    def _attach_line(self, warm: list[relationship.Warmth], noun: str) -> str:
        """Return the whole ledger in one line: how many, and how we act.

        The same two fractions the rows carry, so the reader learns them once:
        of what these people offered us we took this share, and of what we
        took we answered this share with the stronger act. ``warmth`` is the
        Berlyne index over all four factors, and the only place the two we
        measure rather than steer -- how irregular our timing is, how much of
        it arrives in bursts -- are visible at all.

        Blank when the ledger is empty -- a fresh profile has met nobody, and
        the callers drop a blank line rather than print a row of zeroes.
        """
        if not warm:
            return ''
        b = self.bullet()
        seen = sum(w.p for w in warm) / len(warm)
        answered = sum(w.r for w in warm) / len(warm)
        warmth = sum(w.index for w in warm) / len(warm)
        return (
            f'{b} all time {b} {len(warm)} {noun} '
            f'{b} {self._glyph("watched", "w")} {seen:.0%} '
            f'{self._glyph("liked", "l")} {answered:.0%} '
            f'{b} warmth {warmth:.2f}'
        )

    def _warmth_lines(
        self,
        warm: list[relationship.Warmth],
        noun: str,
        known: dict[int, Actor],
    ) -> list[str]:
        """Return one Berlyne ledger's readout: aggregate, then recent peers.

        Exposure p = taken/offered (steered to the Wundt peak ~0.67),
        reciprocity r = recip/taken (steered to 0.20); A~ is the FULL Berlyne
        index over all four factors, so it also carries the two we measure but
        do not steer -- how irregular our timing is and how much of it arrives
        in bursts. That puts A~ in [0, 1.6), not [0, 1]: irregularity is a
        bonus of up to 1.6x. The comment likes and the story views keep the
        same ledger shape, so they read out through one function -- ``noun``
        is all that differs. ``known`` names the peers; the ledger keeps
        only their ids.
        """
        if not warm:
            return []
        b = self.bullet()
        eye, thumb = self._glyph('watched', 'w'), self._glyph('liked', 'l')
        return [
            self._attach_line(warm, noun),
            *(
                f'    {_name(known, w.peer_id)} {b} '
                f'{eye} {w.p:.0%} {thumb} {w.r:.0%}'
                for w in warm[:STATUS_WARM_PEERS]
            ),
        ]

    def _react_attach_lines(self, known: dict[int, Actor]) -> list[str]:
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
        return [
            today,
            *self._warmth_lines(brain.warmth(), 'commenters', known),
        ]

    def _react_rescan_line(self) -> str:
        """Say when the auto-rescan is OFF; its countdown lives in Schedule."""
        if self.bot.comment_watch.deps.rescan_sec > 0:
            return ''
        return f'{self.bullet()} rescan: off (use /requeue)'

    def _pending_react_lines(self) -> list[str]:
        """Return queued reactions: which lands on which comment, when."""
        rows = self.bot.comment_watch.queued_rows()
        return [f'{self.bullet()} queued:', *rows] if rows else []

    def _last_posts_lines(self, known: dict[int, Actor]) -> list[str]:
        """Return the watched comment threads, grouped one line per chat."""
        posts = self.bot.reactions.posts
        if not posts:
            return []
        by_chat: dict[int, list[int]] = {}
        for chat, mid in posts:
            by_chat.setdefault(chat, []).append(mid)
        lines = [f'{self.bullet()} watching {len(posts)} posts:']
        lines.extend(
            f'    {_tag(known, chat)}: {", ".join(str(m) for m in mids)}'
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

    def _stories_lines(self, known: dict[int, Actor]) -> list[str]:
        """Return the story-viewer section: header, the glance, attachment."""
        if not self.bot.stories.params.enabled:
            off = f'{self._dot(on=False)} off'
            return [self._header('stories', 'Stories', off)]
        # Summary first, then this pass: the aggregate is about EVERY person
        # we have ever engaged, the glance about the few with stories up now.
        # Sitting inside the glance block it read as a fourth group of it,
        # which is how a count of 9 came to sit under a heading saying 4.
        return [
            self._stories_line(),
            *([line] if (line := self._attach()) else []),
            *self._glance_lines(known),
        ]

    def _stories_line(self) -> str:
        """Return the story-viewer header: on, counts, the next view."""
        now = time.time()
        tz = self.bot.stories.params.tz_offset_hours
        cap = self.bot.stories.params.react_max_per_day
        parts = [
            f'{self._dot(on=True)} on',
            f'{self.bot.stories.views_today(now, tz)} today',
            f'{self.bot.stories.reacts_today(now, tz)}/{cap} reacted',
        ]
        # Only when it happened: a standing "0 not placed" is noise, while
        # any number here is the difference between a quiet engine and one
        # whose every reaction Telegram is refusing.
        if self.bot.story_watch.unplaced:
            parts.append(f'{self.bot.story_watch.unplaced} not placed')
        parts.append(f'{len(self.bot.story_watch.pending)} queued')
        whens = [v.when for v in self.bot.story_watch.pending]
        if whens:
            due = self._in(min(whens) - now)
            parts.append(f'next view {self.arrow()} {due}')
        return self._header('stories', 'Stories', *parts)

    def _glance_lines(self, known: dict[int, Actor]) -> list[str]:
        """Return who has stories up now, grouped by what we do about them.

        Grouped rather than listed flat, because the question is "who are
        we opening and who are we not", and a header answers it before you
        read a single row. Rendered from the last poll's snapshot with its
        age beside it -- /status must not re-read the feed to look current.
        """
        glance = self.bot.stories.last_glance
        b = self.bullet()
        if not glance.at:
            return [f'{b} glance: none yet']
        head = f'{b} glance {fmt_eta(time.time() - glance.at)} ago {b} '
        if not glance.peers:
            return [head + 'nobody has stories up']
        return [
            head + self._glance_count(glance),
            # What is happening, then what was decided, then what needs
            # nothing -- most actionable first.
            *self._opening_lines(glance, known),
            *self._held_lines(glance, known),
            *self._seen_lines(glance, known),
        ]

    def _attach(self) -> str:
        """Return the story ledger's aggregate line (blank when empty)."""
        return self._attach_line(self.bot.stories.warmth(), 'people')

    def _opening_lines(
        self, glance: stories.Glance, known: dict[int, Actor]
    ) -> list[str]:
        """Return the people whose stories we are opening this glance."""
        rows = [row for row in glance.peers if row.viewing]
        if not rows:
            return []
        b = self.bullet()
        return [
            f'{b} viewing ({len(rows)}):',
            *_capped(
                [
                    f'    {self._who(row, known)} {b} '
                    f'{row.viewing} of {row.unseen} new {b} '
                    f'{self._record(row)}{self._view_eta(row.peer_id)}'
                    for row in sorted(rows, key=lambda r: -r.viewing)
                ]
            ),
        ]

    def _held_lines(
        self, glance: stories.Glance, known: dict[int, Actor]
    ) -> list[str]:
        """Return the people who have something new we are not opening.

        The header carries WHY: a blocked session names its reason once
        (quiet hours, a cooldown, a silent day) instead of repeating it on
        every row, which is the duplication this layout exists to remove.
        """
        rows = [row for row in glance.peers if _waiting(row)]
        if not rows:
            return []
        b = self.bullet()
        why = glance.blocked or 'passed this glance'
        return [
            f'{b} {why} ({len(rows)}):',
            *_capped(
                [
                    f'    {self._who(row, known)} {b} '
                    f'{row.unseen} new {b} {self._record(row)}'
                    for row in sorted(rows, key=lambda r: -r.unseen)
                ]
            ),
        ]

    def _seen_lines(
        self, glance: stories.Glance, known: dict[int, Actor]
    ) -> list[str]:
        """Return the people whose every story we have already opened.

        Nothing is decided about them this pass, which once seemed reason
        enough to reduce them to a count. On a real account it is not: a
        story lives a day and we open it once, so for the rest of that day
        EVERYONE with stories up sits here, and the section about people
        stopped naming any. They carry no "new" number -- what is worth
        showing is who they are, how much they have up, and where we stand
        with them.
        """
        rows = [row for row in glance.peers if not row.unseen]
        if not rows:
            return []
        b = self.bullet()
        return [
            f'{b} already seen ({len(rows)}):',
            *_capped(
                [
                    f'    {self._who(row, known)} {b} '
                    f'{row.active} up {b} {self._record(row)}'
                    for row in sorted(rows, key=lambda r: -r.active)
                ]
            ),
        ]

    def _glance_count(self, glance: stories.Glance) -> str:
        """Return how many people have stories up right now."""
        return f'{len(glance.peers)} with stories'

    def _who(self, row: stories.Seen, known: dict[int, Actor]) -> str:
        """Return a peer's @name, falling back to the bare id, plus a flag."""
        name = _name(known, row.peer_id)
        return f'{name} (archived)' if row.hidden else name

    def _record(self, row: stories.Seen) -> str:
        """Return a peer's all-time record, or say we have no history.

        "first time" rather than 0%: a person we have never engaged reads
        completely differently from one we have been steadily skipping,
        and a bare zero cannot tell them apart.
        """
        held = row.standing
        if not held.offered:
            return 'first time'
        watched = 100 * held.viewed / held.offered
        liked = 100 * held.reacted / held.viewed if held.viewed else 0.0
        eye = self._glyph('watched', 'w')
        thumb = self._glyph('liked', 'l')
        return f'{eye} {watched:.0f}% {thumb} {liked:.0f}%'

    def _view_eta(self, peer_id: int) -> str:
        """Return ' . in ~3m 10s' for a queued peer, '' once it has fired."""
        b = self.bullet()
        for view in self.bot.story_watch.pending:
            if view.peer_id == peer_id:
                eta = view.when - time.time()
                due = 'due now' if eta <= 0 else f'in ~{fmt_eta(eta)}'
                return f' {b} {due}'
        return ''

    def _schedule_lines(self) -> list[str]:
        """Return the Schedule section: when each loop next runs, and the pace.

        Every background loop in one place, because the question an
        operator actually has is "is anything still running?", and six
        countdowns scattered across six sections cannot answer it. A loop
        that is off says so rather than showing a countdown that will
        never reach zero.
        """
        b = self.bullet()
        waiting = self.bot.audience.waiting()
        host = f' {b} '.join(
            (
                f'tick {self.arrow()} {self._due(self.bot.next_tick)}',
                f'probe {self.arrow()} {self._due(self.bot.next_probe())}',
                f'lookups {waiting} queued',
            )
        )
        loops = f' {b} '.join(
            f'{name} {self.arrow()} {self._due(at) if running else "off"}'
            for name, at, running in self._loops()
        )
        return [
            self._header('schedule', 'Schedule'),
            f'{b} {host}',
            f'{b} {loops}',
            *self._pace_lines(),
        ]

    def _loops(self) -> list[tuple[str, float, bool]]:
        """Return (label, next run, running) for each engine's own loop."""
        bot = self.bot
        return [
            (
                'reactions rescan',
                bot.comment_watch.next_rescan,
                bot.reactions.params.enabled
                and bot.comment_watch.deps.rescan_sec > 0,
            ),
            (
                'stories poll',
                bot.story_watch.next_poll,
                bot.stories.params.enabled and bot.stories.params.poll_sec > 0,
            ),
            (
                'greeter check',
                bot.greeter.next_sync,
                bot.greeter.params.enabled,
            ),
        ]

    def _due(self, at: float) -> str:
        """Return a countdown to a scheduled moment; 'first run' before one."""
        return self._in(at - time.time()) if at > 0 else 'first run'

    def _in(self, seconds: float) -> str:
        """Return a countdown, or 'now' for anything under a second.

        Sub-second waits are why the threshold is not zero: fmt_eta floors
        to whole seconds, so half a second rendered as "in 0s" -- which
        reads like a broken counter rather than "free". The gate lands
        there constantly, because resolving the report's own chat names
        uses it moments before the report prints it.
        """
        return 'now' if seconds < 1 else f'in {fmt_eta(seconds)}'

    def _pace_lines(self) -> list[str]:
        """Return the gate's lanes: when each may fire, and any widening.

        The only place a FloodWait is visible without reading the log. A
        lane shows a DURATION, never a clock time: the gate runs on a
        monotonic clock and this report on the wall clock, and mixing the
        two would print a plausible, wrong time.
        """
        lanes = self.bot.account.pacing()
        if not lanes:
            return []
        b = self.bullet()
        ready = f' {b} '.join(
            f'{lane.kind} {self._in(lane.free_in)}' for lane in lanes
        )
        rows = [f'{b} pace {b} {ready}']
        hot = [lane for lane in lanes if lane.slack > 1.0]
        if hot:
            widened = f' {b} '.join(f'{x.kind} x{x.slack:.1f}' for x in hot)
            rows.append(f'{b} widened by a flood {b} {widened}')
        return rows

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
