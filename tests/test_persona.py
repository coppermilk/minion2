# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The shared persona clock (minions/userbot/core/config.py apply_persona).

One account is one person, so a single top-level ``persona`` block is the one
source of truth for the waking window, quiet hours and timezone; it is fanned
into each engine's sub-config via ``setdefault`` (an engine that sets its own
key still wins). These tests pin that fan-out, the derived quiet hours (the
complement of the waking window, for every engine that has them), the
per-engine override, and the no-persona no-op.
"""

from __future__ import annotations

from typing import cast

from minions.userbot.core import config

_TZ = 3
_WAKE_START = 7
_WAKE_END = 17
_OVERRIDE_START = 9
_HOURS = 24
_SILENT = 0.08
_WIDE_START = 5
_WIDE_END = 23


def _block(data: dict[str, object], name: str) -> dict[str, object]:
    """Return one engine sub-config (typed for the assertions)."""
    engines = cast('dict[str, object]', data['engines'])
    return cast('dict[str, object]', engines[name])


def _persona() -> dict[str, object]:
    """Return a persona block with the tz and a 7-17 waking window."""
    return {
        'tz_offset_hours': _TZ,
        'wake_start_hour': _WAKE_START,
        'wake_end_hour': _WAKE_END,
    }


def test_persona_fans_the_clock_into_every_engine() -> None:
    """Tz reaches all four engines; window reaches reactions+greeter."""
    data: dict[str, object] = {'persona': _persona()}
    config.apply_persona(data)
    for name in ('reactions', 'stories', 'greeter', 'comod'):
        assert _block(data, name)['tz_offset_hours'] == _TZ
    reactions = _block(data, 'reactions')
    assert reactions['active_start_hour'] == _WAKE_START
    assert reactions['active_end_hour'] == _WAKE_END
    greeter = _block(data, 'greeter')
    assert greeter['wake_start_hour'] == _WAKE_START
    assert greeter['wake_end_hour'] == _WAKE_END


def test_silent_day_prob_reaches_reactions_and_stories() -> None:
    """One persona silent-day chance fans to both behavioural engines."""
    data: dict[str, object] = {
        'persona': {**_persona(), 'silent_day_prob': _SILENT},
    }
    config.apply_persona(data)
    assert _block(data, 'reactions')['silent_day_prob'] == _SILENT
    assert _block(data, 'stories')['silent_day_prob'] == _SILENT


def test_quiet_hours_are_the_complement_of_the_window() -> None:
    """With no explicit quiet-hours, both engines sleep outside the window."""
    data: dict[str, object] = {'persona': _persona()}
    config.apply_persona(data)
    expected = set(range(_WAKE_START)) | set(range(_WAKE_END, _HOURS))
    for name in ('stories', 'reactions'):
        quiet = _block(data, name)['quiet_hours']
        assert set(cast('list[int]', quiet)) == expected


def test_the_two_engines_are_silent_at_the_same_hours() -> None:
    """One person, one silence -- at ANY window, not just the one shipped.

    Reactions used to keep a hard-coded 2-6 while stories took the
    complement. Those agree only while the window is 7-17, where 2-6 sits
    entirely outside it and the window gate hides the difference. Widened to
    5-23 they diverge at 0, 1, 5, 6 and 23: reactions answers comments at
    01:00 while stories sleeps, and goes quiet at 05:00 while stories
    watches. This asserts the divergence at exactly that window.
    """
    data: dict[str, object] = {
        'persona': {
            **_persona(),
            'wake_start_hour': _WIDE_START,
            'wake_end_hour': _WIDE_END,
        }
    }
    config.apply_persona(data)
    quiet = {
        name: set(cast('list[int]', _block(data, name)['quiet_hours']))
        for name in ('stories', 'reactions')
    }
    assert quiet['reactions'] == quiet['stories']
    assert quiet['stories'] == {0, 1, 2, 3, 4, _WIDE_END}


def test_an_explicit_persona_quiet_list_wins_over_the_window() -> None:
    """A written quiet_hours is the answer for both engines, derived or not."""
    data: dict[str, object] = {
        'persona': {**_persona(), 'quiet_hours': [1, 2]}
    }
    config.apply_persona(data)
    for name in ('stories', 'reactions'):
        assert _block(data, name)['quiet_hours'] == [1, 2]


def test_no_window_leaves_quiet_hours_alone() -> None:
    """A persona with no window says nothing about silence -- not "none".

    The difference is the whole night: an absent key leaves each engine on
    its own declared default, while an empty list written into the config
    means "never silent" and would have the bot answering comments at 3am.
    """
    data: dict[str, object] = {'persona': {'tz_offset_hours': _TZ}}
    config.apply_persona(data)
    for name in ('stories', 'reactions'):
        assert 'quiet_hours' not in _block(data, name)


def test_an_explicit_engine_key_overrides_persona() -> None:
    """A key set in an engine's own section wins; the rest still fills in."""
    data: dict[str, object] = {
        'persona': _persona(),
        'engines': {'reactions': {'active_start_hour': _OVERRIDE_START}},
    }
    config.apply_persona(data)
    reactions = _block(data, 'reactions')
    assert reactions['active_start_hour'] == _OVERRIDE_START  # explicit wins
    assert (
        reactions['active_end_hour'] == _WAKE_END
    )  # still filled from persona


def test_no_persona_block_is_a_noop() -> None:
    """Without a persona block the config is returned untouched."""
    data: dict[str, object] = {'engines': {'reactions': {}}}
    assert config.apply_persona(data) == {'engines': {'reactions': {}}}


def test_one_arc_reaches_both_engines_that_steer_a_person() -> None:
    """The relationship curve is fanned like every other persona trait.

    For the same reason: it is the shape of ONE person's attention to
    somebody over months. Warming to them on stories while going cold on
    their comments is not a mood swing, it is two people -- which is what
    two independently configured arcs would produce.
    """
    arc = [{'name': 'honeymoon', 'days': 14, 'exposure': 1.0, 'recip': 1.0}]
    data = cast(
        'dict[str, object]',
        {'persona': {**_persona(), 'arc': arc, 'arc_enabled': True}},
    )

    config.apply_persona(data)

    for name in ('reactions', 'stories'):
        assert _block(data, name)['arc'] == arc
        assert _block(data, name)['arc_enabled'] is True


def test_an_engine_keeps_an_arc_it_set_for_itself() -> None:
    """setdefault, like every other persona key -- a deliberate exception."""
    mine = [{'name': 'mine', 'days': 1, 'exposure': 0.5, 'recip': 0.5}]
    data = cast(
        'dict[str, object]',
        {
            'persona': {**_persona(), 'arc': [], 'arc_enabled': True},
            'engines': {'stories': {'arc': mine}},
        },
    )

    config.apply_persona(data)

    assert _block(data, 'stories')['arc'] == mine
    assert _block(data, 'reactions')['arc'] == []
