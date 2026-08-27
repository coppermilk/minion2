# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The aggregator's users database (minions/userbot/users.py).

Pure-logic tests against a temp SQLite file: no Telethon, no network. Every
write is idempotent, so re-reads (deferred admin-log events, comment rescans)
never double-count -- that is the property most of these tests pin down.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from minions.userbot.engines.users import Identity
from minions.userbot.engines.users import MembershipEvent
from minions.userbot.engines.users import SeenMessage
from minions.userbot.engines.users import UserStore

_FIRST_SEEN = 100.0
_LAST_SEEN = 200.0
_TOP_COUNT = 3
_TWO_MSGS = 2
_USER_ID = 7

if TYPE_CHECKING:
    from pathlib import Path


def _store(tmp_path: Path) -> UserStore:
    store = UserStore(tmp_path / 'users.db')
    store.clock = lambda: 1000.0  # pinned unless a ts is passed explicitly
    return store


# --- membership timeline


def test_join_leave_rejoin_is_an_ordered_log(tmp_path: Path) -> None:
    """Check join leave rejoin is an ordered log."""
    store = _store(tmp_path)
    store.record_membership(
        MembershipEvent(7, joined=True, left=False, admin_log_id=1, ts=10)
    )
    store.record_membership(
        MembershipEvent(7, joined=False, left=True, admin_log_id=2, ts=20)
    )
    store.record_membership(
        MembershipEvent(7, joined=True, left=False, admin_log_id=3, ts=30)
    )
    hist = store.history(7)
    events = hist['events']
    assert [e['event'] for e in events] == ['join', 'leave', 'join']
    assert [e['ts'] for e in events] == [10, 20, 30]
    assert hist['user']['subscribed'] == 1  # last event was a re-join


def test_leave_sets_subscribed_to_zero(tmp_path: Path) -> None:
    """Check leave sets subscribed to zero."""
    store = _store(tmp_path)
    store.record_membership(
        MembershipEvent(7, joined=True, left=False, admin_log_id=1)
    )
    store.record_membership(
        MembershipEvent(7, joined=False, left=True, admin_log_id=2)
    )
    assert store.summary() == {
        'total': 1,
        'subscribed': 0,
        'left': 1,
        'messages': 0,
    }


def test_membership_is_idempotent_on_admin_log_id(tmp_path: Path) -> None:
    """Check membership is idempotent on admin log id."""
    store = _store(tmp_path)
    assert store.record_membership(
        MembershipEvent(7, joined=True, left=False, admin_log_id=5)
    )
    # the greeter re-reads a deferred event -> same admin_log_id, no new row
    assert not store.record_membership(
        MembershipEvent(7, joined=True, left=False, admin_log_id=5)
    )
    assert len(store.history(7)['events']) == 1


def test_membership_ignores_empty_events(tmp_path: Path) -> None:
    """Check membership ignores empty events."""
    store = _store(tmp_path)
    assert not store.record_membership(
        MembershipEvent(7, joined=False, left=False)
    )
    assert not store.record_membership(
        MembershipEvent(0, joined=True, left=False)
    )
    assert store.summary()['total'] == 0


# --- messages


def test_message_counts_and_dedups(tmp_path: Path) -> None:
    """Check message counts and dedups."""
    store = _store(tmp_path)
    assert store.record_message(SeenMessage(7, -100, 5001, text='hi'))
    assert store.record_message(SeenMessage(7, -100, 5002, text='again'))
    # a rescan re-sees msg 5001 -> same (chat, msg_id), no double count
    assert not store.record_message(SeenMessage(7, -100, 5001, text='hi'))
    assert store.summary()['messages'] == _TWO_MSGS
    assert store.history(7)['user']['msg_count'] == _TWO_MSGS


def test_message_stores_the_text(tmp_path: Path) -> None:
    """Check message stores the text."""
    store = _store(tmp_path)
    store.record_message(
        SeenMessage(7, -100, 5001, root=1002, text='love this one')
    )
    rows = store.top_commenters()
    assert rows[0]['user_id'] == _USER_ID
    assert rows[0]['msg_count'] == 1


def test_first_seen_is_kept_across_updates(tmp_path: Path) -> None:
    """Check first seen is kept across updates."""
    store = _store(tmp_path)
    store.record_message(SeenMessage(7, -100, 5001, text='a', ts=100.0))
    store.record_message(SeenMessage(7, -100, 5002, text='b', ts=200.0))
    user = store.history(7)['user']
    assert user['first_seen'] == _FIRST_SEEN
    assert user['last_seen'] == _LAST_SEEN


# --- identity enrichment


def test_apply_identity_upserts_and_keeps_known_fields(tmp_path: Path) -> None:
    """Check apply identity upserts and keeps known fields."""
    store = _store(tmp_path)
    store.record_message(SeenMessage(7, -100, 5001, text='hi'))
    store.apply_identity(Identity(7, username='alice', first_name='Alice'))
    assert store.has_identity(7)
    # a later enrich with no username must not wipe the known one
    store.apply_identity(Identity(7, first_name='Alice B'))
    user = store.history(7)['user']
    assert user['username'] == 'alice'
    assert user['first_name'] == 'Alice B'
    assert user['phone'] is None  # essentially never available


def test_has_identity_false_before_enrich(tmp_path: Path) -> None:
    """Check has identity false before enrich."""
    store = _store(tmp_path)
    store.record_membership(
        MembershipEvent(7, joined=True, left=False, admin_log_id=1)
    )
    assert not store.has_identity(7)


# --- reporting


def test_top_commenters_orders_by_count(tmp_path: Path) -> None:
    """Check top commenters orders by count."""
    store = _store(tmp_path)
    for i in range(3):
        store.record_message(
            SeenMessage(7, -100, 6000 + i, text='x')
        )  # 3 msgs
    store.record_message(SeenMessage(9, -100, 7000, text='y'))  # 1 msg
    top = store.top_commenters(limit=5)
    assert [r['user_id'] for r in top] == [7, 9]
    assert top[0]['msg_count'] == _TOP_COUNT


def test_recent_events_newest_first(tmp_path: Path) -> None:
    """Check recent events newest first."""
    store = _store(tmp_path)
    store.record_membership(
        MembershipEvent(7, joined=True, left=False, admin_log_id=1, ts=10)
    )
    store.record_membership(
        MembershipEvent(9, joined=True, left=False, admin_log_id=2, ts=20)
    )
    recent = store.recent_events(limit=5)
    assert [r['user_id'] for r in recent] == [9, 7]


def test_summary_of_an_empty_store(tmp_path: Path) -> None:
    """Check summary of an empty store."""
    store = _store(tmp_path)
    assert store.summary() == {
        'total': 0,
        'subscribed': 0,
        'left': 0,
        'messages': 0,
    }


def test_state_survives_reopening_the_file(tmp_path: Path) -> None:
    """Check state survives reopening the file."""
    path = tmp_path / 'users.db'
    store = UserStore(path)
    store.record_membership(
        MembershipEvent(7, joined=True, left=False, admin_log_id=1)
    )
    store.record_message(SeenMessage(7, -100, 5001, text='hi'))
    store.close()
    reopened = UserStore(path)
    assert reopened.summary() == {
        'total': 1,
        'subscribed': 1,
        'left': 0,
        'messages': 1,
    }
