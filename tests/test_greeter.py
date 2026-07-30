"""The subscriber greeter (minions/aggregator/greeter.py).

Telethon-free: the client is duck-typed, so a fake async client drives the
welcome/farewell logic, the silent baseline, and the anti-flood caps.
"""

from __future__ import annotations

import asyncio
import types
from typing import TYPE_CHECKING

from minions.aggregator import greeter

if TYPE_CHECKING:
    from pathlib import Path


def _params(**over):
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


class _FakeClient:
    def __init__(self, members):
        self.members = list(members)
        self.dms = []
        self.fail = {}
        self.names = {}
        self.usernames = {}

    async def get_participants(self, _channel):
        return [types.SimpleNamespace(id=i) for i in self.members]

    async def get_entity(self, uid):
        return types.SimpleNamespace(
            first_name=self.names.get(uid, ''),
            username=self.usernames.get(uid, ''),
        )

    async def send_message(self, uid, text, **_kw):
        exc = self.fail.get(uid)
        if exc is not None:
            raise exc
        self.dms.append((uid, text))


def _greeter(tmp_path, client, **over):
    return greeter.Greeter(client, _params(**over), tmp_path / 'g.json')


def test_diff_members() -> None:
    joined, left = greeter.diff_members({1, 2, 3}, {2, 3, 4})
    assert joined == {4}
    assert left == {1}


def test_first_run_is_a_silent_baseline(tmp_path: Path) -> None:
    client = _FakeClient({1, 2, 3})
    g = _greeter(tmp_path, client)
    asyncio.run(g.sync())
    assert client.dms == []  # the existing members are NEVER greeted
    assert g.state.members == {1, 2, 3}
    assert g.state.started


def test_join_gets_welcome_and_leave_gets_farewell(tmp_path: Path) -> None:
    client = _FakeClient({1, 2})
    g = _greeter(tmp_path, client)
    asyncio.run(g.sync())  # baseline {1, 2}
    client.members = [2, 3]  # 3 joined, 1 left
    asyncio.run(g.sync())
    assert (3, 'hi') in client.dms
    assert (1, 'bye') in client.dms
    assert g.state.members == {2, 3}


def test_returning_subscriber_gets_welcome_back(tmp_path: Path) -> None:
    client = _FakeClient({1, 2})
    g = _greeter(tmp_path, client)
    asyncio.run(g.sync())  # baseline {1, 2}
    client.members = [1]  # 2 left -> remembered
    asyncio.run(g.sync())
    assert (2, 'bye') in client.dms
    assert 2 in g.state.left
    client.members = [1, 2]  # 2 came back
    asyncio.run(g.sync())
    assert (2, 'whale') in client.dms  # welcome_back, not the plain welcome
    assert (2, 'hi') not in client.dms
    assert 2 not in g.state.left


def test_returning_via_live_action_gets_welcome_back(tmp_path: Path) -> None:
    client = _FakeClient({1, 2})
    g = _greeter(tmp_path, client)
    asyncio.run(g.sync())  # baseline -> started
    leave = types.SimpleNamespace(
        user_id=2,
        user_joined=False,
        user_added=False,
        user_left=True,
        user_kicked=False,
    )
    asyncio.run(g.on_action(leave))
    assert (2, 'bye') in client.dms
    rejoin = types.SimpleNamespace(
        user_id=2,
        user_joined=True,
        user_added=False,
        user_left=False,
        user_kicked=False,
    )
    asyncio.run(g.on_action(rejoin))
    assert (2, 'whale') in client.dms
    assert 2 not in g.state.left


def test_name_placeholder_is_filled(tmp_path: Path) -> None:
    client = _FakeClient({1})
    client.names = {7: 'Alice'}
    g = _greeter(tmp_path, client, welcome='hi {name}')
    asyncio.run(g.sync())  # baseline {1}
    client.members = [1, 7]  # 7 joined
    asyncio.run(g.sync())
    assert (7, 'hi Alice') in client.dms


def test_name_placeholder_falls_back_when_unknown(tmp_path: Path) -> None:
    client = _FakeClient({1})  # no name registered for 7
    g = _greeter(tmp_path, client, welcome='hi {name}')
    asyncio.run(g.sync())
    client.members = [1, 7]
    asyncio.run(g.sync())
    assert (7, 'hi friend') in client.dms


def test_sync_now_reports_baseline_then_greets(tmp_path: Path) -> None:
    client = _FakeClient({1, 2})
    g = _greeter(tmp_path, client)
    first = asyncio.run(g.sync_now())  # baseline run
    assert 'baseline' in first
    assert client.dms == []
    client.members = [1, 2, 3]  # 3 joined
    second = asyncio.run(g.sync_now())
    assert '1 DM' in second
    assert (3, 'hi') in client.dms


def test_sync_now_when_members_unreadable(tmp_path: Path) -> None:
    class _NoAdmin(_FakeClient):
        async def get_participants(self, _channel):
            raise RuntimeError('ChatAdminRequiredError')

    g = _greeter(tmp_path, _NoAdmin({1}))
    summary = asyncio.run(g.sync_now())
    assert 'cannot read members' in summary


def test_channel_placeholders_are_filled(tmp_path: Path) -> None:
    client = _FakeClient({1})
    client.usernames = {-100: 'mychan'}  # channel id from _params
    g = _greeter(tmp_path, client, welcome='join {channel} at {channel_url}')
    asyncio.run(g.sync())  # baseline {1}
    client.members = [1, 5]  # 5 joined
    asyncio.run(g.sync())
    assert (5, 'join @mychan at https://t.me/mychan') in client.dms


def test_daily_cap_stops_dms(tmp_path: Path) -> None:
    client = _FakeClient({1})
    g = _greeter(tmp_path, client, max_dm_per_day=2)
    asyncio.run(g.sync())  # baseline
    client.members = [1, 10, 11, 12, 13]  # 4 joined
    asyncio.run(g.sync())
    assert len(client.dms) == 2  # hard daily ceiling


def test_privacy_failure_is_skipped_others_continue(tmp_path: Path) -> None:
    client = _FakeClient({1})
    g = _greeter(tmp_path, client)
    asyncio.run(g.sync())
    client.members = [1, 10, 11]
    client.fail[10] = RuntimeError('UserPrivacyRestrictedError')  # not flood
    asyncio.run(g.sync())
    assert (10, 'hi') not in client.dms
    assert (11, 'hi') in client.dms


def test_flood_aborts_the_whole_cycle(tmp_path: Path) -> None:
    client = _FakeClient({1})
    g = _greeter(tmp_path, client)
    asyncio.run(g.sync())
    client.members = [1, 10, 11, 12]
    for uid in (10, 11, 12):
        client.fail[uid] = RuntimeError('PeerFloodError')  # name carries Flood
    asyncio.run(g.sync())
    assert client.dms == []  # aborted on the first flood, no DMs


def test_empty_farewell_sends_nothing_on_leave(tmp_path: Path) -> None:
    client = _FakeClient({1, 2})
    g = _greeter(tmp_path, client, farewell='')
    asyncio.run(g.sync())
    client.members = [2]  # 1 left
    asyncio.run(g.sync())
    assert not any(uid == 1 for uid, _ in client.dms)


def test_state_persists_members_and_daily_counter(tmp_path: Path) -> None:
    path = tmp_path / 'g.json'
    client = _FakeClient({1, 2})
    g = greeter.Greeter(client, _params(), path)
    asyncio.run(g.sync())
    g.state.dm_today = 3
    g._save()
    fresh = greeter.Greeter(client, _params(), path)
    assert fresh.state.members == {1, 2}
    assert fresh.state.started
    assert fresh.state.dm_today == 3


def test_on_action_join_dms_and_tracks(tmp_path: Path) -> None:
    client = _FakeClient({1, 2})
    g = _greeter(tmp_path, client)
    asyncio.run(g.sync())  # baseline -> started
    event = types.SimpleNamespace(
        user_id=5,
        user_joined=True,
        user_added=False,
        user_left=False,
        user_kicked=False,
    )
    asyncio.run(g.on_action(event))
    assert (5, 'hi') in client.dms
    assert 5 in g.state.members


def test_on_action_before_baseline_does_nothing(tmp_path: Path) -> None:
    client = _FakeClient({1})
    g = _greeter(tmp_path, client)  # never synced -> not started
    event = types.SimpleNamespace(
        user_id=9,
        user_joined=True,
        user_added=False,
        user_left=False,
        user_kicked=False,
    )
    asyncio.run(g.on_action(event))
    assert client.dms == []


def test_load_greeter_params_defaults_off_with_target_channel() -> None:
    params = greeter.load_greeter_params({}, -999)
    assert params.enabled is False
    assert params.channel == -999
