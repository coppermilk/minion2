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
from minions.userbot.core import relationship
from minions.userbot.core import render
from minions.userbot.core.render import Glyphs
from minions.userbot.core.render import emoji_markup
from minions.userbot.core.render import trim
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
_UNIT_KEYS = ('s', 'm', 'h', 'd')
"""Every unit ``fmt_span`` can return, so each gets the operator's letter.

Spelled out rather than discovered, because it is the KEYS of a lookup: a
unit with no entry falls back to its ASCII name, which is how an English
"17h" would slip into a Russian report without anything failing.
"""
TWO_WORDS = 2
"""A /who with no name is the usage line, not a lookup."""

UPTIME_ROWS = 6
"""How many of the busiest learned hours /status names."""

PEOPLE_ROWS = 30
"""How many people /people lists, most recently touched first.

The whole roster can run to hundreds; this is a screen of the ones the
account is actually busy with. Anyone further down is a /who away.
"""

WHO_ROWS = 12
"""How many recent acts /who lists per service.

Enough to see a pattern, short enough to read on a phone. The
counters on the line above are over ALL of them, not just these.
"""

_BELOW = {relationship.NEW: 'new', relationship.MISSED: 'missed'}
"""The two states BELOW the ladder, and the key each looks its word up by.

They are shared across services where the rungs are not: "nothing has been
offered" and "one chance, and it did not land" are the same fact whether the
chance was a story or a comment, so one word serves both. Built from the
constants themselves so a renamed state cannot leave a key behind.
"""

if TYPE_CHECKING:
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

    def glyphs(self) -> Glyphs:
        """Return the report's wording, for a service that renders its rows.

        Built here rather than passed in, because these ARE the report's
        words: a queued reaction rendered by the comment watcher has to come
        out looking like a line of this report, or /reactnow and /status
        describe the same queue two ways.
        """
        return Glyphs(self.bullet(), self.arrow(), self._units())

    def _units(self) -> dict[str, str]:
        """Return the operator's letter for each duration unit."""
        return {unit: self._glyph(f'unit_{unit}', unit) for unit in _UNIT_KEYS}

    def _span(self, seconds: float) -> str:
        """Return a duration the one way this report writes one: '17h', '3d'.

        Every span in every section comes through here -- an age, a
        countdown, a window, a timeout -- because the same number written
        three ways is three things to read.
        """
        return self.glyphs().span(seconds)

    def _clock_eta(self, at: float, tz_offset: float) -> str:
        """Return "HH:MM (in 8h)" for ``at``, read in the persona's zone."""
        zone = timezone(timedelta(hours=tz_offset))
        clock = datetime.fromtimestamp(at, tz=zone).strftime('%H:%M')
        eta = at - time.time()
        return f'{clock} (in {"now" if eta <= 0 else self._span(eta)})'

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
            *self._plan_lines(known),
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

    def people(self) -> str:
        """Return the whole roster: who we know, and what we do with them.

        /status shows the handful with stories up right now, /who shows one
        person's every act. This is the middle one nothing answered: the
        list of everybody, each with the word for what is happening with
        them and where they are on their own curve.

        Ordered by how recently we touched them, so the top of the list is
        who the account is actually busy with.

        The roster is the UNION of the services, not the story engine's
        slice of it: a person we have only ever met in the comments has a
        standing under the other service, and listing one service left them
        out of the list of people entirely.
        """
        db, b = self._db(), self.bullet()
        rows = db.roster(limit=PEOPLE_ROWS)
        if not rows:
            return f'{b} nobody yet'
        known = db.actors([row.peer_id for row in rows])
        return '\n'.join(
            [f'{len(rows)} people, most recently touched first:']
            + [self._person_line(row, known, b) for row in rows]
        )

    def _person_line(
        self, row: PeerRow, known: dict[int, Actor], b: str
    ) -> str:
        """Return one roster line: name, what we do, when, and where.

        One line and one voice: the service that earns the verb also spells
        the swing's face beside it, so the reader is never handed a word
        from one ladder next to a leg named on another. The counts are
        already the fold across services (see ``Database.roster``), because
        a person is one relationship however many ways we reach them.
        """
        service, code = self._reading(row.peer_id)
        fields = [
            _name(known, row.peer_id),
            self._tag(service, code),
            self._act_word(service, code),
            self._ago(row.last_at),
            self._ratio(row.taken, row.offered, row.recip),
            self._leg_of(row.peer_id, service),
        ]
        return f'{b} ' + f' {b} '.join(part for part in fields if part)

    def _tag(self, service: str, code: int) -> str:
        """Return the short name of the service a verb is speaking about.

        The vocabularies do not overlap -- orbiting is only ever a story and
        replying only ever a comment -- but that is a mapping the reader
        would have to learn, and the percentages beside the verb are the
        fold across BOTH services either way. The tag says which ledger
        earned the word; /who is where the split lives.

        Blank when nothing has been offered anywhere: no ledger earned that
        word, so naming one would be the tie-break's arbitrary pick printed
        as a fact -- and it would say "comments" about somebody we have only
        ever seen post a story.
        """
        if code == relationship.NEW:
            return ''
        return self._glyph(f'tag_{service}', service)

    def _ratio(self, taken: int, offered: int, recip: int) -> str:
        """Return the two shares the one way this report writes them: '67/25'.

        Of what they offered we took this much, and of what we took we
        answered this much. Written the same in every section: the roster
        said one thing with an eye and a thumb on every row, the story
        section another with bracketed letters, and they were the same two
        numbers. The glyphs move to the legend, where one copy explains all
        of them.
        """
        seen = 100 * taken / offered if offered else 0.0
        back = 100 * recip / taken if taken else 0.0
        return f'{seen:.0f}/{back:.0f}'

    def _ago(self, at: float) -> str:
        """Return '3d' for the last time we touched somebody, or 'never'.

        A verb with no moment is a claim with nothing behind it: "liking"
        reads as something happening, and without a date the reader cannot
        tell it from something that happened in March. The word says WHAT,
        this says WHEN, and only together are they checkable.

        No "ago" on it: the column after the verb is the only place a bare
        duration appears on the line, so the word would be on every row and
        carry nothing -- and the row is read across, not parsed.
        """
        if at <= 0:
            return 'never'
        return self._span(max(0.0, time.time() - at))

    def _leg_of(self, peer_id: int, service: str = 'stories') -> str:
        """Return 'honeymoon, round 2' for a peer, or 'no arc' when it is off.

        ``service`` names the ladder a swing's face is spelled in, and the
        caller passes whichever one it just spoke the verb in -- so a line
        reads in one voice. The leg itself does not depend on it: the arc is
        anchored on ``met()``, which is one day per person whichever engine
        asks, so both services put the same person in the same leg on the
        same evening.
        """
        brain = getattr(self.bot, service)
        control = brain._control()  # noqa: SLF001 -- the arc is its own config
        if not control.arc.enabled:
            return 'no arc'
        since, now = brain.store.met(peer_id), brain.clock()
        leg = control.arc.leg(since, now, peer_id)
        name = control.leg_name(service, leg)
        return f'{name} {control.arc.rounds(since, now)}'

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

        One line for the person, not one per service: the arc is anchored on
        ``met()``, which is the day we first did ANYTHING with them, so both
        ladders below it are walking the same leg on the same evening. The
        swing's face is spelled in the ladder of whichever service we are
        doing the most in, the same one the roster names them by.
        """
        service, _ = self._reading(peer_id)
        brain = getattr(self.bot, service)
        control = brain._control()  # noqa: SLF001 -- the arc is its own config
        if not control.arc.enabled:
            return []
        since = brain.store.met(peer_id)
        now = brain.clock()
        leg = control.arc.leg(since, now, peer_id)
        met = _when(since) if since > 0 else 'just now'
        return [
            f'{b} {control.leg_name(service, leg)}, '
            f'round {control.arc.rounds(since, now)} '
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
            f'DMs {self.bot.greeter.dms_today()}/{gp.max_dm_per_day}',
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

        The next wake is under a day away by construction, so the span
        beside the clock time is hours at its coarsest.
        """
        gp = self.bot.greeter.params
        local = humanize.local(now, gp.tz_offset_hours)
        wake = local.replace(
            hour=int(gp.wake_start_hour), minute=0, second=0, microsecond=0
        )
        if wake <= local:
            wake += timedelta(days=1)
        return self._clock_eta(wake.timestamp(), gp.tz_offset_hours)

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
        time_part = self._span(secs) if secs > 0 else 'off'
        count_part = f'last {count}' if count > 0 else 'off'
        if secs <= 0 and count <= 0:
            return 'off'
        return f'{time_part}/{count_part}'

    def _videos_lines(self) -> list[str]:
        """Videos: counts on the header, then pending + recent posts."""
        b = self.bullet()
        window = self._span(self.bot.config.timeout)
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
                f' {self.arrow()} ~{self._span(left)}'
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
        seen = 100 * sum(w.p for w in warm) / len(warm)
        answered = 100 * sum(w.r for w in warm) / len(warm)
        warmth = sum(w.index for w in warm) / len(warm)
        return (
            f'{b} all time {b} {len(warm)} {noun} '
            f'{b} {seen:.0f}/{answered:.0f} '
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
        return [
            self._attach_line(warm, noun),
            *(
                f'    {_name(known, w.peer_id)} {b} '
                f'{100 * w.p:.0f}/{100 * w.r:.0f}'
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

    def _plan_lines(self, known: dict[int, Actor]) -> list[str]:
        """Return everything the bot is about to do, both engines, by time.

        One queue, because the operator's question is "what is this bot
        about to do", not "what is each engine about to do". The comment
        reactions were listed with their etas and the planned story views
        were not listed at all -- they only showed as a count in another
        section's header -- so a reader could see half the plan and had no
        way to know the other half existed.

        Sorted by the moment each fires, which is the only order that makes
        two queues one, and capped after the merge so the cut falls on the
        furthest-out item rather than on whichever engine was rendered
        second.
        """
        rows = [
            *self.bot.comment_watch.queued_rows(),
            *self._planned_views(known),
        ]
        head = self._header('plan', 'Plan', f'{len(rows)} queued')
        if not rows:
            return [head, f'{self.bullet()} nothing scheduled']
        return [head, *_capped([row for _, row in sorted(rows)])]

    def _planned_views(
        self, known: dict[int, Actor]
    ) -> list[tuple[float, str]]:
        """Return the story views scheduled for this session, as (when, row).

        Says how many of whose stories, and whether a reaction rides along:
        the plan is decided when the session is laid out, so this is a
        promise the reader can hold the engine to rather than a guess.
        """
        now = time.time()
        tag = self._glyph('tag_stories', 'stories')
        b = self.bullet()
        return [
            (
                view.when,
                f'    {tag} {b} {len(view.story_ids)} stories '
                f'{_name(known, view.peer_id) or view.peer_id}'
                f'{f" + {view.react_emoji}" if view.react_ids else ""}'
                f' {b} {self._due_in(view.when - now)}',
            )
            for view in self.bot.story_watch.pending
        ]

    def _due_in(self, seconds: float) -> str:
        """Return 'due now' or 'in ~3h' -- how a queued row says when."""
        return 'due now' if seconds <= 0 else f'in ~{self._span(seconds)}'

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
        age = self._span(time.time() - glance.at)
        head = f'{b} glance {age} ago {b} '
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
                    f'{self._record(row)}'
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
            f'{b} {why}{self._lifts()} ({len(rows)}):',
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
        """Return what we are doing with a peer, then their all-time record.

        The word first, because it is the question: percentages say what has
        happened on average and the verb says what is happening NOW, and
        somebody eleven days into a cold shoulder reads as neglected in the
        numbers without it ever saying so.

        "first time" rather than 0%: a person we have never engaged reads
        completely differently from one we have been steadily skipping,
        and a bare zero cannot tell them apart.

        The STORY word, not the person's strongest anywhere: this line sits
        in the story section beside story percentages, and a verb borrowed
        from the comments would be the contradiction all over again.
        """
        doing = self._doing('stories', row.peer_id)
        held = row.standing
        if not held.offered:
            return f'{doing} {self.bullet()} first time'
        ratio = self._ratio(held.viewed, held.offered, held.reacted)
        return f'{doing} {self.bullet()} {ratio}'

    def _codes(self, peer_id: int) -> dict[str, tuple[int, int]]:
        """Return ``{service: (rung, offered)}`` for every ledger we keep.

        One arc, walked by two services. ``Control.doing`` answers per
        service because the record is per service, so this asks each one and
        the callers below decide whether they want a single service's answer
        or the strongest of them.

        Each engine is reachable by its own service name -- ``bot.stories``
        keeps ``'stories'`` -- and ``ACTS`` names exactly the services that
        keep a ledger, so the pair drives the loop instead of a second list
        that could fall out of step with the first.

        Each standing is read through that engine's OWN store rather than
        through one shared file, because a service can be sandboxed into its
        own profile while the other stays live, and then its record really
        does live somewhere else.
        """
        found = {}
        for service in ACTS:
            brain = getattr(self.bot, service)
            control = brain._control()  # noqa: SLF001 -- the arc is its config
            row = brain.store.peer(peer_id)
            leg = brain.ledger.leg(peer_id, control, brain.clock())
            found[service] = (control.doing(leg, row), row.offered)
        return found

    def _reading(self, peer_id: int) -> tuple[str, int]:
        """Return the strongest thing we do with a person, and where.

        A person is one relationship -- the arc is anchored on ``met()``,
        which is deliberately not bound to a service -- so the roster says
        one word about them, and the honest one is the most we do anywhere.
        ``Control.doing`` counts rungs rather than naming them precisely so
        that "the most" is ``max`` and not a table of cases.

        Ties go to the service we have more record with, and a dead heat to
        the earlier name: at that point both words are equally true, and
        picking stably matters more than picking cleverly.
        """
        codes = self._codes(peer_id)
        best = max(codes, key=lambda s: codes[s])
        return best, codes[best][0]

    def _act_word(self, service: str, code: int) -> str:
        """Return the operator's word for one rung of one service's ladder.

        Below the ladder the word is shared: "nothing has happened yet" and
        "one chance, and it did not land" are the same fact whichever
        service was offering. On the ladder it is not -- ``like`` is the top
        of what we ever do to a story and the middle of what we do to a
        comment -- so the key carries the service and the two cannot collide.

        The words live in the constants JSON, so this file stays ASCII and
        the vocabulary stays the operator's.
        """
        below = _BELOW.get(code)
        if below is not None:
            return self._glyph(f'act_{below}', below)
        act = ACTS[service][code]
        return self._glyph(f'act_{service}_{act}', act)

    def _doing(self, service: str, peer_id: int) -> str:
        """Return the word for ONE service -- what its own section is about."""
        return self._act_word(service, self._codes(peer_id)[service][0])

    def doing(self, peer_id: int) -> str:
        """Return the one word for what we are doing with somebody now.

        The lesser of what this person's leg INTENDS and what has actually
        happened with them, across every service -- see ``Control.doing``.
        Claiming "liking" beside a 0% like column was not two views of one
        thing, it was a contradiction, and it printed the same word for
        everybody besides, since an intention is a property of the leg and a
        fresh account has everybody in the same leg.
        """
        return self._act_word(*self._reading(peer_id))

    def _lifts(self) -> str:
        """Return ' -> in 8h 12m' for a blocked session, '' when it is not.

        A reason on its own says the engine is waiting and nothing about how
        long, and the difference between a cooldown and a silent day is
        minutes against most of a day. Read fresh rather than from the
        glance, which may be an hour old by the time anyone looks.
        """
        brain = self.bot.stories
        if not brain.blocked_reason():
            return ''
        lifts = brain.blocked_until()
        if lifts is None:
            return ''
        return f' {self.arrow()} {self._in(lifts - time.time())}'

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

        Sub-second waits are why the threshold is not zero: ``fmt_span``
        floors to whole seconds, so half a second read "in 0s" -- which
        reads like a broken counter rather than "free". The gate lands
        there constantly, because resolving the report's own chat names
        uses it moments before the report prints it.
        """
        return 'now' if seconds < 1 else f'in {self._span(seconds)}'

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
