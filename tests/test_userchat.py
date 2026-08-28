# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The Telegram boundary: typed values in, typed values out, paced.

Runs against a fake client and never imports telethon -- which is itself
the point being tested. Every raw-request path is exercised elsewhere; here
we pin the three promises the adapter makes to everything above it: it
returns values rather than duck-typed objects, it degrades to an empty
answer instead of raising, and no request leaves without passing the gate.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
from types import SimpleNamespace

from minion_core.adapters import userchat
from minion_core.pace import Gate
from minion_core.pace import Pace

CHAT = -1001
MSG = 77
POST = 42
TWO = 2


@dataclass
class _Counting(Gate):
    """A gate that records what asked to pass, and never sleeps."""

    seen: list[str] = field(default_factory=list)

    async def wait(self, kind: str) -> None:
        """Record the kind instead of pacing it."""
        self.seen.append(kind)


def _gate() -> _Counting:
    """Return a gate that lets everything through, counting kinds."""
    return _Counting({}, Pace())


async def _answer(value: object) -> object:
    """Return ``value``, as an awaitable the fake client can hand back."""
    return value


async def _boom(_: object = None) -> object:
    """Fail the way an unreachable Telegram does."""
    msg = 'connection dead'
    raise OSError(msg)


def _account(**calls: object) -> userchat.Account:
    """Build an Account over a fake client exposing ``calls``."""
    return userchat.Account(SimpleNamespace(**calls), _gate())


def _message(**over: object) -> SimpleNamespace:
    """Return a Telethon-shaped message stub."""
    base = {
        'id': MSG,
        'chat_id': CHAT,
        'message': 'hi',
        'out': False,
        'sender_id': 5,
        'reply_to': SimpleNamespace(reply_to_msg_id=1, reply_to_top_id=9),
        'date': None,
        'reactions': None,
    }
    return SimpleNamespace(**{**base, **over})


# ------------------------------------------------------------- the values


def test_a_first_level_comment_still_finds_its_post() -> None:
    """Msg.root falls back to reply_to_msg_id -- the common comment shape.

    A nested comment carries reply_to_top_id; a first-level one carries
    only reply_to_msg_id, and it names the same post. Reading just the
    first would root every top-level comment at 0, and the like engine
    matches a comment to a post BY that root -- so it would quietly stop
    answering exactly the comments people write most.
    """
    flat = _message(reply_to=SimpleNamespace(reply_to_msg_id=POST))
    account = _account(get_messages=lambda *a, **k: _answer([flat]))
    got = asyncio.run(account.history(CHAT, userchat.Slice(limit=1)))
    assert got[0].root == POST
    assert got[0].reply_to == POST


def test_a_message_that_answers_nothing_has_no_root() -> None:
    """A plain message roots at 0, so it can never match a tracked post."""
    account = _account(
        get_messages=lambda *a, **k: _answer([_message(reply_to=None)])
    )
    got = asyncio.run(account.history(CHAT, userchat.Slice(limit=1)))
    assert got[0].root == 0


def test_a_message_becomes_a_typed_msg() -> None:
    """The 16 getattr reads this replaces collapse into one conversion."""
    account = _account(get_messages=lambda *a, **k: _answer([_message()]))
    got = asyncio.run(account.history(CHAT, userchat.Slice(limit=1)))
    assert got == [
        userchat.Msg(
            id=MSG,
            chat_id=CHAT,
            text='hi',
            sender_id=5,
            reply_to=1,
            root=9,
        )
    ]


def test_our_own_reaction_is_read_from_the_tally() -> None:
    """chosen_order is the reliable "we already reacted here" signal.

    recent_reactions is capacity-capped, so it is only the fallback -- a
    popular comment pushes our entry out of it.
    """
    tally = SimpleNamespace(
        results=[SimpleNamespace(chosen_order=0)], recent_reactions=[]
    )
    assert userchat._mine(tally) is True
    old = SimpleNamespace(
        results=[SimpleNamespace(chosen_order=None)],
        recent_reactions=[SimpleNamespace(my=True)],
    )
    assert userchat._mine(old) is True
    assert userchat._mine(None) is False


def test_an_entity_becomes_a_typed_peer() -> None:
    """Missing name fields read as empty strings, never as None."""
    raw = SimpleNamespace(id=5, username='alice', title=None)
    account = _account(get_entity=lambda _: _answer(raw))
    got = asyncio.run(account.peer(5))
    assert got == userchat.Peer(id=5, username='alice')


def test_an_admin_log_row_becomes_a_member_event() -> None:
    """joined_invite counts as a join, as the greeter has always assumed."""
    row = SimpleNamespace(id=3, user_id=7, joined_invite=True, left=False)
    assert userchat._event(row) == userchat.MemberEvent(
        id=3, user_id=7, joined=True
    )


# --------------------------------------------------------- the degradation


def test_a_failed_read_is_an_empty_list_not_an_exception() -> None:
    """Callers deal in values; the transport's failure stops here."""
    account = _account(get_messages=lambda *a, **k: _boom())
    assert asyncio.run(account.history(CHAT, userchat.Slice())) == []


def test_a_failed_lookup_is_none() -> None:
    """An unresolvable peer is None, so the caller can show the raw id."""
    account = _account(get_entity=lambda _: _boom())
    assert asyncio.run(account.peer(5)) is None


def test_a_failed_send_reports_zero() -> None:
    """A post that did not go out returns 0, so the caller can re-queue."""
    account = _account(send_message=lambda *a, **k: _boom())
    assert asyncio.run(account.send(CHAT, userchat.Text('x'))) == 0


def test_a_flood_widens_the_gate() -> None:
    """A FloodWait is fed back into the pace, not just logged."""

    async def flood(*_: object, **__: object) -> object:
        raise FloodWaitError

    class FloodWaitError(Exception):
        """Named as Telethon names it, since that is what is matched."""

    account = _account(get_entity=flood)
    asyncio.run(account.peer(5))
    assert account.gate.slack(userchat.READ) > 1.0


# ---------------------------------------------------------------- the gate


def test_every_request_kind_passes_the_gate() -> None:
    """Nothing leaves without asking first -- the whole point of one door."""
    account = _account(
        get_me=lambda: _answer(SimpleNamespace(id=1)),
        get_entity=lambda _: _answer(SimpleNamespace(id=1)),
        get_messages=lambda *a, **k: _answer([]),
        send_message=lambda *a, **k: _answer(SimpleNamespace(id=2)),
    )

    async def go() -> None:
        await account.me()
        await account.peer(5)
        await account.history(CHAT, userchat.Slice())
        await account.send(CHAT, userchat.Text('x'))
        await account.dm(5, userchat.Text('x'))

    asyncio.run(go())
    gate: _Counting = account.gate  # type: ignore[assignment]
    assert gate.seen == [
        userchat.PROBE,
        userchat.READ,
        userchat.READ,
        userchat.WRITE,
        userchat.DM,
    ]


def test_dm_is_paced_apart_from_ordinary_writes() -> None:
    """DMs from a user account are the top ban trigger, so they get a lane."""
    gate = userchat.paces({})
    assert gate.paces[userchat.DM].min_gap_sec > (
        gate.paces[userchat.WRITE].min_gap_sec
    )
    assert gate.paces[userchat.DM].per_minute < (
        gate.paces[userchat.WRITE].per_minute
    )
