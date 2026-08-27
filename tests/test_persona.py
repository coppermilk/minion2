# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The shared persona clock (minions/userbot/core/config.py apply_persona).

One account is one person, so a single top-level ``persona`` block is the one
source of truth for the waking window, quiet hours and timezone; it is fanned
into each engine's sub-config via ``setdefault`` (an engine that sets its own
key still wins). These tests pin that fan-out, the derived stories quiet-hours
(the complement of the waking window), the per-engine override, and the
no-persona no-op.
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


def _block(data: dict[str, object], name: str) -> dict[str, object]:
    """Return one engine sub-config (typed for the assertions)."""
    return cast('dict[str, object]', data[name])


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


def test_stories_quiet_hours_are_the_complement_of_the_window() -> None:
    """With no explicit quiet-hours, stories sleep outside the wake window."""
    data: dict[str, object] = {'persona': _persona()}
    config.apply_persona(data)
    quiet = _block(data, 'stories')['quiet_hours']
    expected = set(range(_WAKE_START)) | set(range(_WAKE_END, _HOURS))
    assert set(cast('list[int]', quiet)) == expected


def test_an_explicit_engine_key_overrides_persona() -> None:
    """A key set in an engine's own section wins; the rest still fills in."""
    data: dict[str, object] = {
        'persona': _persona(),
        'reactions': {'active_start_hour': _OVERRIDE_START},
    }
    config.apply_persona(data)
    reactions = _block(data, 'reactions')
    assert reactions['active_start_hour'] == _OVERRIDE_START  # explicit wins
    assert (
        reactions['active_end_hour'] == _WAKE_END
    )  # still filled from persona


def test_no_persona_block_is_a_noop() -> None:
    """Without a persona block the config is returned untouched."""
    data: dict[str, object] = {'reactions': {}}
    assert config.apply_persona(data) == {'reactions': {}}
