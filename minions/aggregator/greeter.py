# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Welcome / farewell DMs for channel subscribers (opt-in, rate-limited).

RISKS -- read before enabling. DMing subscribers from a USER account is the top
trigger for Telegram spam-bans: enough DMs and Telegram returns PeerFloodError
and limits the account. Many DMs also simply fail (the person closed their DMs
or is not a contact -> UserPrivacyRestricted). DMing someone who UNSUBSCRIBED
is worse on both counts. Treat this as best-effort and low-volume.

Detection uses the channel's ADMIN LOG (Recent Actions), not the member list:
``iter_admin_log`` streams join/leave events with a monotonic id, so it is not
capped at ~200 like the member list of a broadcast channel, and it catches
subscribe/unsubscribe reliably where a user account never sees a live event.
We track the id of the last handled event (a high-water mark) and only act on
newer ones. The account must be an ADMIN (to read the log); the log retains
events for a limited window (a few days), so a very long outage may miss some.

Safety built in here:
    * The FIRST run is a silent baseline -- it records the newest event id and
      greets nobody, so enabling it never mass-DMs the backlog.
    * Every DM is rate-limited (a human-like gap + jitter); each cycle is
      capped (max_dm_per_run) and each day is capped (max_dm_per_day).
    * Over-cap events are NOT marked handled, so they are retried on a later
      poll (tomorrow, once the daily counter resets) -- nobody is dropped.
    * Privacy failures are skipped; a flood error backs off the cycle.
    * Disabled by default.

Telethon-free (the client is passed in and duck-typed), so the event-handling
logic is unit-testable. All texts live in the constants JSON, keeping this
source ASCII.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import time
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

log = logging.getLogger('aggregator')


@dataclass(frozen=True)
class GreeterParams:
    """Every greeter tunable, from the constants JSON 'greeter' section."""

    enabled: bool
    channel: int  # the channel to watch (resolved: JSON value or the target)
    welcome: str
    welcome_back: str  # for someone who LEFT and later re-subscribed
    farewell: str
    fallback_name: str  # fills {name} when the real name is unresolvable
    poll_sec: float
    dm_min_gap_sec: float
    dm_jitter_sec: float
    max_dm_per_run: int
    max_dm_per_day: int  # hard daily ceiling -- the real anti-ban knob


@dataclass
class GreeterState:
    """Persisted: welcome-back memory, baseline flag, cursor, DM counter."""

    left: set[int] = field(default_factory=set)  # unsubscribed -> welcome_back
    started: bool = False  # the silent baseline has been established
    last_event_id: int = 0  # highest admin-log event id already handled
    dm_day: str = ''  # UTC date of dm_today
    dm_today: int = 0  # DMs already sent today (against max_dm_per_day)


class _FloodStopError(Exception):
    """Raised to abort a DM cycle when Telegram flags us for flooding."""


def _norm_event(event: object) -> tuple[int, int, bool, bool]:
    """(event_id, user_id, joined, left) from a Telethon AdminLogEvent."""
    joined = bool(
        getattr(event, 'joined', False)
        or getattr(event, 'joined_invite', False)
    )
    return (
        int(getattr(event, 'id', 0) or 0),
        int(getattr(event, 'user_id', 0) or 0),
        joined,
        bool(getattr(event, 'left', False)),
    )


def load_greeter_params(
    data: dict[str, object], default_channel: int
) -> GreeterParams:
    """Load the greeter params; channel falls back to the aggregator target."""
    cfg = data.get('greeter') if isinstance(data.get('greeter'), dict) else {}
    cfg = cfg or {}
    return GreeterParams(
        enabled=bool(cfg.get('enabled', False)),
        channel=int(cfg.get('channel') or default_channel),
        welcome=str(cfg.get('welcome') or 'Welcome!'),
        welcome_back=str(cfg.get('welcome_back') or ''),
        farewell=str(cfg.get('farewell') or ''),
        fallback_name=str(cfg.get('fallback_name') or 'friend'),
        poll_sec=float(cfg.get('poll_sec') or 600.0),
        dm_min_gap_sec=float(cfg.get('dm_min_gap_sec') or 30.0),
        dm_jitter_sec=float(cfg.get('dm_jitter_sec') or 30.0),
        max_dm_per_run=int(cfg.get('max_dm_per_run') or 10),
        max_dm_per_day=int(cfg.get('max_dm_per_day') or 5),
    )


class Greeter:
    """Watch a channel's members and DM joiners/leavers (safely, opt-in).

    ``client`` is a Telethon client (duck-typed); ``path`` is the state file.
    """

    def __init__(  # noqa: PLR0913, PLR0917 -- client + params + path + sink
        self,
        client: object,
        params: GreeterParams,
        path: Path,
        on_event: Callable[[tuple[int, int, bool, bool]], None] | None = None,
    ) -> None:
        """Bind the client, params, state path and event sink."""
        self.client = client
        self.params = params
        self.path = path
        self.state = self._load()
        self._last_dm = 0.0
        self._channel_at = ''  # '@username' cache for {channel}
        self._channel_url = ''  # 't.me/username' cache for {channel_url}
        self.next_sync = 0.0  # epoch of the next scheduled poll (0 = not set)
        # Optional sink for EVERY fetched admin-log event (admin_log_id,
        # user_id, joined, left) -- the users DB taps this. Fired even during
        # the silent baseline and on re-reads; the DB dedups on admin_log_id.
        self._on_event = on_event

    async def sync(self) -> None:
        """Poll the admin log; baseline on first run, else greet new events."""
        if not self.params.enabled or not self.params.channel:
            return
        events = await self._fetch_events()
        if events is None:
            return
        self._emit(events)  # feed the users DB before any baseline/DM logic
        if not self.state.started:
            newest = max(
                (eid for eid, *_ in events), default=self.state.last_event_id
            )
            self.state.last_event_id = newest
            self.state.started = True
            self._save()
            log.info('greeter: baseline at event %d (no greetings)', newest)
            return
        if events:
            await self._process_events(events)

    async def sync_now(self) -> str:
        """Force a poll right now; return a one-line summary for /greetnow."""
        if not self.params.enabled or not self.params.channel:
            return 'greeter: disabled'
        before = self.state.dm_today
        started_before = self.state.started
        await self.sync()
        if not self.state.started:
            return 'greeter: cannot read admin log (admin?)'
        cursor = self.state.last_event_id
        if not started_before:
            return f'greeter: baseline at event {cursor} (no DMs sent)'
        sent = self.state.dm_today - before
        return f'greeter: {sent} DM(s) sent (up to event {cursor})'

    async def loop(self) -> None:
        """Poll the admin log forever at ``poll_sec``."""
        while True:
            try:
                await self.sync()
            except Exception:
                log.exception('greeter: sync failed')
            self.next_sync = time.time() + self.params.poll_sec
            await asyncio.sleep(self.params.poll_sec)

    def _emit(self, events: list[tuple[int, int, bool, bool]]) -> None:
        """Hand every fetched event to the sink (the users DB), if wired.

        Fired for ALL events -- baseline and re-reads included -- so the DB
        captures membership even on the greeter's silent first run; the sink
        dedups on ``admin_log_id``. A sink failure never disturbs greeting.
        """
        if self._on_event is None:
            return
        for event in events:
            try:
                self._on_event(event)
            except Exception:
                log.exception(
                    'greeter: users sink failed for event %d', event[0]
                )

    async def _fetch_events(self) -> list[tuple[int, int, bool, bool]] | None:
        """New join/leave events (id > cursor) from the admin log, or None."""
        try:
            return [
                _norm_event(event)
                async for event in self.client.iter_admin_log(
                    self.params.channel,
                    min_id=self.state.last_event_id,
                    join=True,
                    leave=True,
                )
            ]
        except Exception:  # noqa: BLE001 -- not admin / unreachable: skip cycle
            log.warning(
                'greeter: cannot read admin log of %s (admin?)',
                self.params.channel,
            )
            return None

    async def _process_events(
        self, events: list[tuple[int, int, bool, bool]]
    ) -> None:
        """Greet each new event oldest-first; defer the rest when the cap hits.

        ``last_event_id`` advances only past events we actually handle, so a
        cap or flood leaves the rest to be re-read on a later poll (tomorrow,
        once the daily counter resets) -- the cursor IS the queue.
        """
        sent = 0
        for eid, uid, joined, left in sorted(events):
            try:
                did = await self._handle(uid, joined=joined, left=left)
            except _FloodStopError:
                log.warning('greeter: cap hit; the rest wait for a later poll')
                break
            self.state.last_event_id = max(self.state.last_event_id, eid)
            sent += int(did)
            if sent >= self.params.max_dm_per_run:
                break
        self._save()

    async def _handle(self, uid: int, *, joined: bool, left: bool) -> bool:
        """DM a joiner/leaver; return whether a DM went out (updates left)."""
        if uid <= 0 or not (joined or left):
            return False
        if joined:
            sent = await self._dm(uid, self._welcome_text(uid))
            self.state.left.discard(uid)  # they came back
            return sent
        self.state.left.add(uid)  # remember for a welcome_back later
        if not self.params.farewell:
            return False
        return await self._dm(uid, self.params.farewell)

    def _welcome_text(self, uid: int) -> str:
        """A returning subscriber gets welcome_back (falls back to welcome)."""
        if uid in self.state.left:
            return self.params.welcome_back or self.params.welcome
        return self.params.welcome

    async def _dm(self, uid: int, text: str) -> bool:
        """One rate-limited DM; False on a skip, raise _FloodStopError to stop.

        Stops (raises _FloodStopError) when the daily ceiling is reached or
        Telegram flags a flood -- the strongest guard against a spam-ban.
        """
        if not self._daily_budget_left():
            log.info('greeter: daily DM cap reached, stopping for today')
            raise _FloodStopError
        await self._rate_limit()
        body = await self._personalize(uid, text)
        try:
            # parse_mode='html' renders <tg-emoji> emoji and <a> links;
            # link_preview=False stops the return link expanding a preview.
            await self.client.send_message(
                uid, body, parse_mode='html', link_preview=False
            )
        except Exception as exc:
            name = type(exc).__name__
            log.warning('greeter: DM to %s failed (%s)', uid, name)
            if 'Flood' in name:
                raise _FloodStopError from exc
            return False
        self.state.dm_today += 1
        self._save()
        log.info(
            'greeter: DM sent to %s (%d/%d today)',
            uid,
            self.state.dm_today,
            self.params.max_dm_per_day,
        )
        return True

    async def _personalize(self, uid: int, text: str) -> str:
        """Fill {name} and the channel placeholders in the message text."""
        if '{name}' in text:
            text = text.replace('{name}', await self._first_name(uid))
        if '{channel}' in text or '{channel_url}' in text:
            at, url = await self._channel_links()
            # {channel_url} first: {channel} is a substring of it.
            text = text.replace('{channel_url}', url).replace('{channel}', at)
        return text

    async def _channel_links(self) -> tuple[str, str]:
        """(@username, t.me/username) for the channel, resolved once."""
        if self._channel_at or self._channel_url:
            return self._channel_at, self._channel_url
        username = ''
        try:
            entity = await self.client.get_entity(self.params.channel)
            username = str(getattr(entity, 'username', '') or '')
        except Exception:  # noqa: BLE001 -- unresolvable: leave placeholders empty
            log.warning('greeter: cannot resolve channel username')
        if username:
            self._channel_at = '@' + username
            self._channel_url = 'https://t.me/' + username
        return self._channel_at, self._channel_url

    async def _first_name(self, uid: int) -> str:
        """The user's first name (HTML-escaped), or the fallback name."""
        try:
            entity = await self.client.get_entity(uid)
        except Exception:  # noqa: BLE001 -- unresolvable name: use the fallback
            return html.escape(self.params.fallback_name)
        raw = str(getattr(entity, 'first_name', '') or '').strip()
        return html.escape(raw or self.params.fallback_name)

    def _daily_budget_left(self) -> bool:
        """Whether we may still DM today (resets the counter on a new date)."""
        today = time.strftime('%Y-%m-%d', time.gmtime())
        if today != self.state.dm_day:
            self.state.dm_day = today
            self.state.dm_today = 0
        return self.state.dm_today < self.params.max_dm_per_day

    async def _rate_limit(self) -> None:
        """Wait a human-like gap since the last DM (anti-flood pacing)."""
        gap = self.params.dm_min_gap_sec + random.uniform(  # noqa: S311
            0.0, self.params.dm_jitter_sec
        )
        wait = self._last_dm + gap - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_dm = time.time()

    def _load(self) -> GreeterState:
        """Reload state, or start fresh on a channel switch / an old file.

        The file records the channel it belongs to; a mismatch means the
        cursor is for a DIFFERENT channel, so we drop it and re-baseline.
        A pre-admin-log file (member-diff format: has 'members', no
        'last_event_id') is also re-baselined -- keeping its cursor at 0 would
        re-read the whole admin log and mass-DM. The welcome_back memory
        (``left``) is carried over in that migration.
        """
        if not self.path.exists():
            return GreeterState()
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return GreeterState()
        stored = int(raw.get('channel', 0) or 0)
        if stored and stored != self.params.channel:
            log.warning(
                'greeter: state was for channel %s, now %s -- re-baselining',
                stored,
                self.params.channel,
            )
            return GreeterState()
        carried = {int(m) for m in (raw.get('left') or [])}
        if 'last_event_id' not in raw:
            log.info('greeter: migrating to admin-log, re-baselining')
            return GreeterState(left=carried)
        return GreeterState(
            left=carried,
            started=bool(raw.get('started', False)),
            last_event_id=int(raw.get('last_event_id', 0) or 0),
            dm_day=str(raw.get('dm_day', '')),
            dm_today=int(raw.get('dm_today', 0)),
        )

    def _save(self) -> None:
        """Persist the state atomically as readable JSON."""
        data = {
            'channel': self.params.channel,  # which channel this state is for
            'left': sorted(self.state.left),
            'started': self.state.started,
            'last_event_id': self.state.last_event_id,
            'dm_day': self.state.dm_day,
            'dm_today': self.state.dm_today,
        }
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        tmp.replace(self.path)
