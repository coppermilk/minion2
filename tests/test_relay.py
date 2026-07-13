"""relay: each media bot gets its own start-acknowledgement text."""

from __future__ import annotations

from minions.telegram.relay import _ACKS


def test_each_media_bot_has_its_own_ack() -> None:
    """The six relay bots each get a distinct, non-empty ack."""
    assert set(_ACKS) == {
        'censor-blur',
        'censor-black',
        'restore',
        'frames',
        'fetch',
        'fan-save',
    }
    assert all(_ACKS.values())  # none empty
    assert len(set(_ACKS.values())) == len(_ACKS)  # each distinct
