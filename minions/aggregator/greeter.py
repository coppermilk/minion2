"""Welcome / farewell DMs for channel subscribers (opt-in, rate-limited).

RISKS -- read before enabling. DMing subscribers from a USER account is the top
trigger for Telegram spam-bans: enough DMs and Telegram returns PeerFloodError
and limits the account. Many DMs also simply fail (the person closed their DMs
or is not a contact -> UserPrivacyRestricted). DMing someone who UNSUBSCRIBED
is worse on both counts. Treat this as best-effort and low-volume.

Safety built in here:
    * The FIRST run is a silent baseline -- the existing member list is stored,
      nobody is greeted. Only people who join AFTER that are greeted, so
      enabling it never mass-DMs your whole channel.
    * Every DM is rate-limited (a human-like gap + jitter) and each cycle is
      capped (max_dm_per_run).
    * Privacy failures are skipped; a flood error aborts the cycle (back off).
    * Disabled by default; the account must be an ADMIN of the channel to read
      its members.

Telethon-free (the client is passed in and duck-typed), so the diff/baseline
logic is unit-testable. All texts live in the constants JSON, keeping this
source ASCII.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
    poll_sec: float
    dm_min_gap_sec: float
    dm_jitter_sec: float
    max_dm_per_run: int
    max_dm_per_day: int  # hard daily ceiling -- the real anti-ban knob


@dataclass
class GreeterState:
    """Persisted: member set, baseline flag, and the daily DM counter."""

    members: set[int] = field(default_factory=set)
    left: set[int] = field(default_factory=set)  # unsubscribed -> welcome_back
    started: bool = False  # the silent baseline has been established
    dm_day: str = ''  # UTC date of dm_today
    dm_today: int = 0  # DMs already sent today (against max_dm_per_day)


class _FloodStop(Exception):
    """Raised to abort a DM cycle when Telegram flags us for flooding."""


def diff_members(old: set[int], new: set[int]) -> tuple[set[int], set[int]]:
    """(joined, left) between two member snapshots."""
    return new - old, old - new


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

    def __init__(
        self, client: object, params: GreeterParams, path: Path
    ) -> None:
        self.client = client
        self.params = params
        self.path = path
        self.state = self._load()
        self._last_dm = 0.0

    async def sync(self) -> None:
        """Poll the member list; baseline on first run, else DM the diff."""
        if not self.params.enabled or not self.params.channel:
            return
        current = await self._fetch_members()
        if current is None:
            return
        if not self.state.started:
            self.state.members = current
            self.state.started = True
            self._save()
            log.info(
                'greeter: baseline %d members (no greetings sent)',
                len(current),
            )
            return
        joined, left = diff_members(self.state.members, current)
        self.state.members = current
        self._save()
        if joined or left:
            log.info('greeter: +%d joined, -%d left', len(joined), len(left))
            await self._process(joined, left)

    async def on_action(self, event: object) -> None:
        """A live join/leave (Telethon ChatAction) -- DM once, idempotently."""
        if not self.params.enabled or not self.state.started:
            return  # never act before the baseline exists
        uid = getattr(event, 'user_id', None)
        if uid is None:
            return
        uid = int(uid)
        joined = getattr(event, 'user_joined', False) or getattr(
            event, 'user_added', False
        )
        left = getattr(event, 'user_left', False) or getattr(
            event, 'user_kicked', False
        )
        if joined and uid not in self.state.members:
            text = self._join_text(uid, self.state.left)
            await self._live_dm(uid, text, add=True)
        elif left and uid in self.state.members:
            await self._live_dm(uid, self.params.farewell, add=False)

    async def loop(self) -> None:
        """Poll forever at ``poll_sec`` (a safety net for missed events)."""
        while True:
            try:
                await self.sync()
            except Exception:
                log.exception('greeter: sync failed')
            await asyncio.sleep(self.params.poll_sec)

    async def _live_dm(self, uid: int, text: str, *, add: bool) -> None:
        """Update the member set for a live event, then DM (skip if empty)."""
        if add:
            self.state.members.add(uid)
            self.state.left.discard(uid)  # they came back
        else:
            self.state.members.discard(uid)
            self.state.left.add(uid)  # remember for a welcome_back later
        self._save()
        if text:
            try:
                await self._dm(uid, text)
            except _FloodStop:
                log.warning('greeter: flood on live DM, backing off')

    async def _process(self, joined: set[int], left: set[int]) -> None:
        """DM the joiners (welcome / welcome_back) then the leavers, capped."""
        returning = joined & self.state.left
        try:
            budget = await self._greet_joiners(
                joined, returning, self.params.max_dm_per_run
            )
            if self.params.farewell:
                await self._greet(left, self.params.farewell, budget)
        except _FloodStop:
            log.warning('greeter: flood limit hit, backing off this cycle')
        # Remember who left (welcome_back later) and forget who came back.
        self.state.left = (self.state.left - joined) | left
        self._save()

    def _join_text(self, uid: int, returning: set[int]) -> str:
        """A returning subscriber gets welcome_back (falls back to welcome)."""
        if uid in returning:
            return self.params.welcome_back or self.params.welcome
        return self.params.welcome

    async def _greet_joiners(
        self, ids: set[int], returning: set[int], budget: int
    ) -> int:
        """DM up to ``budget`` joiners, welcome_back for returning ones."""
        for uid in list(ids):
            if budget <= 0:
                break
            if await self._dm(uid, self._join_text(uid, returning)):
                budget -= 1
        return budget

    async def _greet(self, ids: set[int], text: str, budget: int) -> int:
        """DM up to ``budget`` of ``ids``; return the remaining budget."""
        for uid in list(ids):
            if budget <= 0:
                break
            if await self._dm(uid, text):
                budget -= 1
        return budget

    async def _dm(self, uid: int, text: str) -> bool:
        """One rate-limited DM; False on a skip, raise _FloodStop to stop.

        Stops (raises _FloodStop) when the daily ceiling is reached or Telegram
        flags a flood -- the strongest guard against a spam-ban.
        """
        if not self._daily_budget_left():
            log.info('greeter: daily DM cap reached, stopping for today')
            raise _FloodStop
        await self._rate_limit()
        try:
            await self.client.send_message(uid, text)
        except Exception as exc:
            name = type(exc).__name__
            log.warning('greeter: DM to %s failed (%s)', uid, name)
            if 'Flood' in name:
                raise _FloodStop from exc
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

    async def _fetch_members(self) -> set[int] | None:
        """The channel's current member ids, or None if unreadable."""
        try:
            users = await self.client.get_participants(self.params.channel)
        except Exception:  # noqa: BLE001 -- not admin / unreachable: skip cycle
            log.warning(
                'greeter: cannot read members of %s (admin?)',
                self.params.channel,
            )
            return None
        return {int(getattr(u, 'id', 0) or 0) for u in users} - {0}

    def _load(self) -> GreeterState:
        """Reload the persisted member set, or start fresh."""
        if not self.path.exists():
            return GreeterState()
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return GreeterState()
        return GreeterState(
            members={int(m) for m in (raw.get('members') or [])},
            left={int(m) for m in (raw.get('left') or [])},
            started=bool(raw.get('started', False)),
            dm_day=str(raw.get('dm_day', '')),
            dm_today=int(raw.get('dm_today', 0)),
        )

    def _save(self) -> None:
        """Persist the member set atomically as readable JSON."""
        data = {
            'members': sorted(self.state.members),
            'left': sorted(self.state.left),
            'started': self.state.started,
            'dm_day': self.state.dm_day,
            'dm_today': self.state.dm_today,
        }
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        tmp.replace(self.path)
