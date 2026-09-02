# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The subscriber greeter (minions/userbot/engines/greeter.py).

Telethon-free: a fake async client under a real Account drives the admin-log
detection, the silent baseline, the welcome/farewell logic and the anti-flood
caps. The Account is real rather than stubbed so these tests also cover the
conversions the greeter now depends on -- an admin-log row into a MemberEvent,
a FloodWait into a widened gate.
"""

from __future__ import annotations

import asyncio
import types
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Never

from minion_core.adapters import userchat
from minion_core.pace import Gate
from minion_core.pace import Pace
from minions.userbot.core import statefile
from minions.userbot.engines import greeter

_CAP_PER_CYCLE = 2
_CHANNEL_ID = -999
_DM_TODAY = 3
_EVENT_2 = 2
_EVENT_3 = 3
_THREE_DMS = 3
_TWO_DMS = 2
_UID_5 = 5

if TYPE_CHECKING:
    from pathlib import Path


def _params(**over: object) -> object:
    base = {
        'enabled': True,
        'channel': -100,
        'welcome': 'hi',
        'welcome_back': 'whale',
        'farewell': 'bye',
        'fallback_name': 'friend',
        'poll_sec': 600.0,
        'dm_min_gap_sec': 0.0,
        'dm_jitter_sec': 0.0,
        'max_dm_per_run': 10,
        'max_dm_per_day': 5,
        'tz_offset_hours': 0.0,
        'wake_start_hour': 0.0,
        'wake_end_hour': 24.0,
    }
    base.update(over)
    return greeter.GreeterParams(**base)


def _join(eid: int, uid: int) -> object:
    return types.SimpleNamespace(
        id=eid, user_id=uid, joined=True, joined_invite=False, left=False
    )


def _leave(eid: int, uid: int) -> object:
    return types.SimpleNamespace(
        id=eid, user_id=uid, joined=False, joined_invite=False, left=True
    )


class _PeerFloodError(Exception):
    """Its type name contains 'Flood', which greeter._dm treats as a flood."""


async def _aiter(items: list[object]) -> object:
    for item in items:
        yield item


class _FakeClient:
    def __init__(self, log: object = ()) -> None:
        self.log = list(log)  # admin-log events (SimpleNamespace)
        self.dms = []
        self.fail = {}
        self.names = {}
        self.usernames = {}

    def iter_admin_log(
        self,
        _channel: object,
        *,
        min_id: int = 0,
        join: object = False,
        leave: object = False,
    ) -> object:
        events = [e for e in self.log if e.id > min_id]
        return _aiter(events)

    async def get_entity(self, uid: int) -> object:
        return types.SimpleNamespace(
            first_name=self.names.get(uid, ''),
            username=self.usernames.get(uid, ''),
        )

    async def send_message(self, uid: int, text: str, **_kw: object) -> object:
        exc = self.fail.get(uid)
        if exc is not None:
            raise exc
        self.dms.append((uid, text))
        # Telethon answers a send with the Message it created, and the
        # adapter reads "did it go out" off that answer.
        return types.SimpleNamespace(id=len(self.dms))


def _account(client: object) -> userchat.Account:
    """Wrap a fake client in a real, unpaced Account."""
    return userchat.Account(client, Gate({}, Pace()))


def _greeter(tmp_path: Path, client: object, **over: object) -> object:
    return greeter.Greeter(
        _account(client),
        _params(**over),
        greeter.GreeterIO(tmp_path / 'g.json'),
    )


def test_on_event_sink_fires_for_every_event_incl_baseline(
    tmp_path: Path,
) -> None:
    # The users DB taps this sink: it must see every fetched event as a
    # MemberEvent -- even on the silent baseline run where nobody is
    # greeted.
    """Check on event sink fires for every event incl baseline."""
    seen: list[userchat.MemberEvent] = []
    client = _FakeClient([_join(1, 100), _leave(2, 100)])
    g = greeter.Greeter(
        _account(client),
        _params(),
        greeter.GreeterIO(tmp_path / 'g.json', seen.append),
    )
    asyncio.run(g.sync())  # baseline: no DMs, but the sink still fires
    assert client.dms == []
    assert seen == [
        userchat.MemberEvent(1, 100, joined=True),
        userchat.MemberEvent(2, 100, left=True),
    ]


def test_first_run_is_a_silent_baseline(tmp_path: Path) -> None:
    """Check first run is a silent baseline."""
    client = _FakeClient([_join(1, 100), _join(2, 101)])
    g = _greeter(tmp_path, client)
    asyncio.run(g.sync())
    assert client.dms == []  # the backlog is NEVER greeted
    assert g.state.started
    assert g.state.last_event_id == _EVENT_2  # cursor at the newest event


def test_join_gets_welcome_and_leave_gets_farewell(tmp_path: Path) -> None:
    """Check join gets welcome and leave gets farewell."""
    client = _FakeClient([_join(1, 100)])
    g = _greeter(tmp_path, client)
    asyncio.run(g.sync())  # baseline at event 1
    client.log += [_join(2, 3), _leave(3, 100)]
    asyncio.run(g.sync())
    assert (3, 'hi') in client.dms
    assert (100, 'bye') in client.dms
    assert g.state.last_event_id == _EVENT_3


def test_returning_subscriber_gets_welcome_back(tmp_path: Path) -> None:
    """Check returning subscriber gets welcome back."""
    client = _FakeClient([_join(1, 1)])
    g = _greeter(tmp_path, client)
    asyncio.run(g.sync())  # baseline
    client.log += [_leave(2, 5)]
    asyncio.run(g.sync())
    assert (5, 'bye') in client.dms
    assert _UID_5 in g.state.left
    client.log += [_join(3, 5)]  # 5 comes back
    asyncio.run(g.sync())
    assert (5, 'whale') in client.dms  # welcome_back, not the plain welcome
    assert (5, 'hi') not in client.dms
    assert _UID_5 not in g.state.left


def test_name_placeholder_is_filled(tmp_path: Path) -> None:
    """Check name placeholder is filled."""
    client = _FakeClient([_join(1, 1)])
    client.names = {7: 'Alice'}
    g = _greeter(tmp_path, client, welcome='hi {name}')
    asyncio.run(g.sync())  # baseline
    client.log += [_join(2, 7)]
    asyncio.run(g.sync())
    assert (7, 'hi Alice') in client.dms


def test_name_placeholder_falls_back_when_unknown(tmp_path: Path) -> None:
    """Check name placeholder falls back when unknown."""
    client = _FakeClient([_join(1, 1)])
    g = _greeter(tmp_path, client, welcome='hi {name}')
    asyncio.run(g.sync())
    client.log += [_join(2, 7)]  # no name registered for 7
    asyncio.run(g.sync())
    assert (7, 'hi friend') in client.dms


def test_channel_placeholders_are_filled(tmp_path: Path) -> None:
    """Check channel placeholders are filled."""
    client = _FakeClient([_join(1, 1)])
    client.usernames = {-100: 'mychan'}  # channel id from _params
    g = _greeter(tmp_path, client, welcome='join {channel} at {channel_url}')
    asyncio.run(g.sync())
    client.log += [_join(2, 5)]
    asyncio.run(g.sync())
    assert (5, 'join @mychan at https://t.me/mychan') in client.dms


def test_over_cap_events_wait_for_the_next_day(tmp_path: Path) -> None:
    """Check over cap events wait for the next day."""
    client = _FakeClient([_join(1, 1)])
    g = _greeter(tmp_path, client, max_dm_per_day=1, max_dm_per_run=10)
    asyncio.run(g.sync())  # baseline at event 1
    client.log += [_join(2, 10), _join(3, 11)]  # 2 joins, cap is 1
    asyncio.run(g.sync())
    assert len(client.dms) == 1  # one greeted today
    assert (
        g.state.last_event_id == _EVENT_2
    )  # cursor advanced only past the greeted
    g.state.dm_today = 0  # a new day resets the counter
    asyncio.run(g.sync())
    assert len(client.dms) == _TWO_DMS  # the deferred event is greeted now
    assert g.state.last_event_id == _EVENT_3


def test_per_run_cap_limits_one_cycle(tmp_path: Path) -> None:
    """Check per run cap limits one cycle."""
    client = _FakeClient([_join(1, 1)])
    g = _greeter(tmp_path, client, max_dm_per_run=2, max_dm_per_day=100)
    asyncio.run(g.sync())  # baseline
    client.log += [_join(2, 10), _join(3, 11), _join(4, 12)]
    asyncio.run(g.sync())
    assert len(client.dms) == _CAP_PER_CYCLE  # capped per cycle
    asyncio.run(g.sync())  # the rest on the next poll
    assert len(client.dms) == _THREE_DMS


def test_privacy_failure_is_skipped_but_committed(tmp_path: Path) -> None:
    """Check privacy failure is skipped but committed."""
    client = _FakeClient([_join(1, 1)])
    g = _greeter(tmp_path, client)
    asyncio.run(g.sync())
    client.fail[10] = RuntimeError('UserPrivacyRestrictedError')
    client.log += [_join(2, 10), _join(3, 11)]
    asyncio.run(g.sync())
    assert (10, 'hi') not in client.dms
    assert (11, 'hi') in client.dms
    assert (
        g.state.last_event_id == _EVENT_3
    )  # privacy is committed, not retried


def test_flood_defers_the_rest(tmp_path: Path) -> None:
    """Check flood defers the rest."""
    client = _FakeClient([_join(1, 1)])
    g = _greeter(tmp_path, client)
    asyncio.run(g.sync())
    client.log += [_join(2, 10), _join(3, 11)]
    for uid in (10, 11):
        client.fail[uid] = _PeerFloodError()
    asyncio.run(g.sync())
    assert client.dms == []  # aborted on the first flood
    assert g.state.last_event_id == 1  # cursor NOT advanced -> retried later


def test_empty_farewell_sends_nothing_on_leave(tmp_path: Path) -> None:
    """Check empty farewell sends nothing on leave."""
    client = _FakeClient([_join(1, 1)])
    g = _greeter(tmp_path, client, farewell='')
    asyncio.run(g.sync())
    client.log += [_leave(2, 5)]
    asyncio.run(g.sync())
    assert not any(uid == _UID_5 for uid, _ in client.dms)
    assert _UID_5 in g.state.left  # still remembered for a welcome_back
    assert g.state.last_event_id == _EVENT_2  # committed


def test_cannot_read_admin_log_is_reported(tmp_path: Path) -> None:
    """Check cannot read admin log is reported."""

    class _NoAdmin(_FakeClient):
        def iter_admin_log(self, _channel: object, **_kw: object) -> Never:
            msg = 'ChatAdminRequiredError'
            raise RuntimeError(msg)

    g = _greeter(tmp_path, _NoAdmin())
    summary = asyncio.run(g.sync_now())
    assert 'cannot read admin log' in summary


def test_sync_now_reports_baseline_then_dms(tmp_path: Path) -> None:
    """Check sync now reports baseline then dms."""
    client = _FakeClient([_join(1, 1)])
    g = _greeter(tmp_path, client)
    first = asyncio.run(g.sync_now())
    assert 'baseline' in first
    client.log += [_join(2, 5)]
    second = asyncio.run(g.sync_now())
    assert '1 DM' in second
    assert (5, 'hi') in client.dms


def test_state_persists_cursor_and_counter(tmp_path: Path) -> None:
    """Check state persists cursor and counter."""
    path = tmp_path / 'g.json'
    client = _FakeClient([_join(1, 1)])
    g = greeter.Greeter(_account(client), _params(), greeter.GreeterIO(path))
    asyncio.run(g.sync())
    g.state.dm_today = 3
    g._save()
    fresh = greeter.Greeter(
        _account(client), _params(), greeter.GreeterIO(path)
    )
    assert fresh.state.started
    assert fresh.state.last_event_id == 1
    assert fresh.state.dm_today == _DM_TODAY


def test_channel_switch_resets_the_baseline(tmp_path: Path) -> None:
    """Check channel switch resets the baseline."""
    path = tmp_path / 'g.json'
    client = _FakeClient([_join(9, 1)])
    g = greeter.Greeter(
        _account(client), _params(channel=-100), greeter.GreeterIO(path)
    )
    asyncio.run(g.sync())  # baseline on channel -100 (cursor 9)
    assert g.state.started
    g2 = greeter.Greeter(
        _account(client), _params(channel=-200), greeter.GreeterIO(path)
    )
    assert g2.state.started is False  # different channel -> re-baseline
    assert g2.state.last_event_id == 0


def test_old_member_state_migrates_and_rebaselines(tmp_path: Path) -> None:
    """Check old member state migrates and rebaselines."""
    path = tmp_path / 'greeter.db'
    # An old member-diff state (has 'members', no 'last_event_id').
    statefile.write_state(
        path,
        {
            'channel': -100,
            'members': [1, 2, 3],
            'left': [9],
            'started': True,
        },
    )
    g = greeter.Greeter(
        _FakeClient(), _params(channel=-100), greeter.GreeterIO(path)
    )
    assert g.state.started is False  # re-baseline (no mass DM)
    assert g.state.last_event_id == 0
    assert g.state.left == [9]  # welcome_back memory carried over


def test_load_greeter_params_defaults_off_with_target_channel() -> None:
    """Check load greeter params defaults off with target channel."""
    params = greeter.load_greeter_params({}, -999)
    assert params.enabled is False
    assert params.channel == _CHANNEL_ID


def _at_hour(hour: int) -> float:
    """Return a UTC timestamp at wall-clock ``hour`` (tz offset 0 in tests)."""
    return datetime(1970, 1, 1, hour, tzinfo=UTC).timestamp()


def test_awake_only_inside_the_waking_window(tmp_path: Path) -> None:
    """The persona sleeps outside its wake window (no 4am welcome DMs)."""
    g = _greeter(
        tmp_path, _FakeClient(), wake_start_hour=7.0, wake_end_hour=17.0
    )
    assert g.awake(_at_hour(3)) is False  # pre-dawn
    assert g.awake(_at_hour(12)) is True  # midday
    assert g.awake(_at_hour(22)) is False  # night
    # A 0..24 window (the persona-less default) is always awake.
    always = _greeter(
        tmp_path, _FakeClient(), wake_start_hour=0.0, wake_end_hour=24.0
    )
    assert always.awake(_at_hour(3)) is True


def test_sleep_defers_events_until_wake(tmp_path: Path) -> None:
    """A join outside the window is deferred; the cursor does not advance."""
    client = _FakeClient([_join(1, 100)])
    g = greeter.Greeter(
        _account(client),
        _params(wake_start_hour=7.0, wake_end_hour=17.0),
        greeter.GreeterIO(tmp_path / 'g.json'),
    )
    asyncio.run(g.sync())  # baseline
    client.log.append(_join(2, 200))
    g.awake = lambda _now: False  # type: ignore[method-assign]
    asyncio.run(g.sync())
    assert client.dms == []  # asleep: nobody greeted
    assert g.deferred == 1  # and the queue is visible
    g.awake = lambda _now: True  # type: ignore[method-assign]
    asyncio.run(g.sync())
    assert [uid for uid, _text in client.dms] == [200]  # re-read on wake
    assert g.deferred == 0  # queue drained


def test_sync_now_reports_asleep_with_deferred_count(tmp_path: Path) -> None:
    """/greetnow while asleep says so and how many events wait."""
    client = _FakeClient([_join(1, 100)])
    g = greeter.Greeter(
        _account(client),
        _params(wake_start_hour=7.0, wake_end_hour=17.0),
        greeter.GreeterIO(tmp_path / 'g.json'),
    )
    asyncio.run(g.sync())  # baseline
    client.log.append(_join(2, 200))
    g.awake = lambda _now: False  # type: ignore[method-assign]
    summary = asyncio.run(g.sync_now())
    assert 'asleep' in summary
    assert g.deferred == 1
    assert client.dms == []


# --- the welcome_back memory is bounded -----------------------------------

_OVER_LEFT_CAP = greeter.LEFT_CAP + 100


def test_departures_stop_growing_forever(tmp_path: Path) -> None:
    """The welcome_back memory is capped, newest kept.

    Every subscriber who ever unsubscribed used to be persisted for good, in
    a channel whose whole business is turnover.
    """
    g = greeter.Greeter(
        _FakeClient(),
        _params(channel=-100),
        greeter.GreeterIO(tmp_path / 'greeter_state.json'),
    )
    for uid in range(_OVER_LEFT_CAP):
        g._note_departure(uid)
    assert len(g.state.left) == greeter.LEFT_CAP
    assert g.state.left[-1] == _OVER_LEFT_CAP - 1  # the newest survived
    assert 0 not in g.state.left  # the oldest rolled off


def test_a_returning_subscriber_is_forgotten_once(tmp_path: Path) -> None:
    """Coming back drops the departure; leaving again re-adds it, once."""
    g = greeter.Greeter(
        _FakeClient(),
        _params(channel=-100),
        greeter.GreeterIO(tmp_path / 'greeter_state.json'),
    )
    g._note_departure(7)
    g._note_departure(7)  # the same person leaving twice is one memory
    assert g.state.left == [7]
    g._forget_departure(7)
    assert g.state.left == []
