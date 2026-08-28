# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The JSON readers: what a key means when the file is not what we hoped.

These five functions stand between hand-editable JSON and every engine's
settings, so their fallbacks ARE the bot's behaviour on a malformed file.
The awkward cases are the point: a number typed as a string, a null where
a list belongs, a key that is simply absent.
"""

from __future__ import annotations

from dataclasses import dataclass

from minions.userbot.core import codec

FALLBACK = 7.0
WHOLE_FALLBACK = 7
STRING_NUMBER = '77'
EXPECTED = 77
TRUNCATED = 3
FRACTION = 1.5


def test_a_number_typed_as_a_string_is_still_a_number() -> None:
    """The state files are readable JSON so a person can edit them.

    A person types "77" as readily as 77, and the old readers took it --
    they called int() and float() directly. Refusing it here would not be
    strict, it would be a cursor silently reset to zero: the greeter's
    last_event_id at 0 re-reads the whole admin log and DMs everyone in
    it, and an aggregator msg_id at 0 breaks the re-post guard.
    """
    assert codec.whole(STRING_NUMBER) == EXPECTED
    assert codec.num(STRING_NUMBER) == float(EXPECTED)
    assert codec.num('1.5') == FRACTION


def test_what_is_not_a_number_reads_as_the_fallback() -> None:
    """Absent, null, a word, a list -- all mean "the key is not set"."""
    for bad in (None, 'nope', [], {}, ''):
        assert codec.num(bad, FALLBACK) == FALLBACK
        assert codec.whole(bad, WHOLE_FALLBACK) == WHOLE_FALLBACK


def test_a_float_truncates_the_way_int_did() -> None:
    """whole() is int(), so 3.7 is 3 -- not 4, and not a refusal."""
    assert codec.whole(3.7) == TRUNCATED


def test_containers_answer_empty_rather_than_raising() -> None:
    """A wrong-shaped value reads as "nothing here", never as a crash."""
    assert codec.rows([1, 2]) == [1, 2]
    assert codec.rows(None) == []
    assert codec.rows({'a': 1}) == []
    assert codec.table({'a': 1}) == {'a': 1}
    assert codec.table(None) == {}
    assert codec.table([1]) == {}


def test_text_reads_a_missing_key_as_its_fallback() -> None:
    """A null key is unset, not the four letters of "None"."""
    assert codec.text(None, 'x') == 'x'
    assert codec.text(0) == '0'
    assert codec.text('hi') == 'hi'


@dataclass(frozen=True)
class _Knobs:
    """A stand-in engine params block, one field per reader."""

    count: int = 1
    ratio: float = 0.5
    label: str = 'off'


def test_decode_reads_string_numbers_off_a_section() -> None:
    """The dataclass-is-the-schema path inherits the same tolerance."""
    got = codec.decode(_Knobs, {'count': '4', 'ratio': '0.25'})
    assert got == _Knobs(count=4, ratio=0.25, label='off')
