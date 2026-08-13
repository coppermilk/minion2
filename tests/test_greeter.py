# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The subscriber greeter (minions/aggregator/greeter.py).

Telethon-free: the client is duck-typed, so a fake async client drives the
admin-log detection, the silent baseline, the welcome/farewell logic and the
anti-flood caps.
"""

from __future__ import annotations

import asyncio
import json
import types
from typing import TYPE_CHECKING
from typing import Never

from minions.aggregator import greeter

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

    async def send_message(self, uid: int, text: str, **_kw: object) -> None:
        exc = self.fail.get(uid)
        if exc is not None:
            raise exc
        self.dms.append((uid, text))


def _greeter(tmp_path: Path, client: object, **over: object) -> object:
    return greeter.Greeter(client, _params(**over), tmp_path / 'g.json')


def test_on_event_sink_fires_for_every_event_incl_baseline(
    tmp_path: Path,
) -> None:
    # The users DB taps this sink: it must see every fetched event as a
    # (admin_log_id, user_id, joined, left) tuple -- even on the silent
    # baseline run where nobody is greeted.
    """Check on event sink fires for every event incl baseline."""
    seen: list[tuple[int, int, bool, bool]] = []
    client = _FakeClient([_join(1, 100), _leave(2, 100)])
    g = greeter.Greeter(client, _params(), tmp_path / 'g.json', seen.append)
    asyncio.run(g.sync())  # baseline: no DMs, but the sink still fires
    assert client.dms == []
    assert seen == [(1, 100, True, False), (2, 100, False, True)]


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
    g = greeter.Greeter(client, _params(), path)
    asyncio.run(g.sync())
    g.state.dm_today = 3
    g._save()
    fresh = greeter.Greeter(client, _params(), path)
    assert fresh.state.started
    assert fresh.state.last_event_id == 1
    assert fresh.state.dm_today == _DM_TODAY


def test_channel_switch_resets_the_baseline(tmp_path: Path) -> None:
    """Check channel switch resets the baseline."""
    path = tmp_path / 'g.json'
    client = _FakeClient([_join(9, 1)])
    g = greeter.Greeter(client, _params(channel=-100), path)
    asyncio.run(g.sync())  # baseline on channel -100 (cursor 9)
    assert g.state.started
    g2 = greeter.Greeter(client, _params(channel=-200), path)
    assert g2.state.started is False  # different channel -> re-baseline
    assert g2.state.last_event_id == 0


def test_old_member_state_migrates_and_rebaselines(tmp_path: Path) -> None:
    """Check old member state migrates and rebaselines."""
    path = tmp_path / 'g.json'
    # An old member-diff state file (has 'members', no 'last_event_id').
    path.write_text(
        json.dumps(
            {
                'channel': -100,
                'members': [1, 2, 3],
                'left': [9],
                'started': True,
            }
        ),
        encoding='utf-8',
    )
    g = greeter.Greeter(_FakeClient(), _params(channel=-100), path)
    assert g.state.started is False  # re-baseline (no mass DM)
    assert g.state.last_event_id == 0
    assert g.state.left == {9}  # welcome_back memory carried over


def test_load_greeter_params_defaults_off_with_target_channel() -> None:
    """Check load greeter params defaults off with target channel."""
    params = greeter.load_greeter_params({}, -999)
    assert params.enabled is False
    assert params.channel == _CHANNEL_ID
