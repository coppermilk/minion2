"""llm adapter tests: JSON verdict parsing and model spec wiring."""

from __future__ import annotations

import json

import pytest

from minion_core.adapters.llm import LlmError
from minion_core.adapters.llm import _parse_classification
from minion_core.adapters.llm import _text_of
from minion_core.adapters.llm import spec_from

VERDICT = {
    'fandom': 'HarryPotter',
    'character': 'Snape',
    'layer': 'Fg',
    'location': 'office',
    'emotion': 'angry',
    'filename': 'FgSnapeOfficeAngry',
    'description': 'Snape glares in his office.',
    'confidence': 'high',
    'censored': False,
}


def test_parse_happy_path() -> None:
    got = _parse_classification(json.dumps(VERDICT))
    assert got.fandom == 'HarryPotter'
    assert got.filename == 'FgSnapeOfficeAngry'
    assert got.censored is False
    assert got.confidence == 'high'


def test_parse_strips_markdown_fence() -> None:
    text = '```json\n' + json.dumps(VERDICT) + '\n```'
    assert _parse_classification(text).filename == 'FgSnapeOfficeAngry'


def test_parse_garbage_raises_llm_error() -> None:
    with pytest.raises(LlmError):
        _parse_classification('the model rambled instead')
    with pytest.raises(LlmError):
        _parse_classification('[1, 2, 3]')
    with pytest.raises(LlmError):
        _parse_classification('')


def test_parse_sanitizes_names_to_prims() -> None:
    dirty = dict(VERDICT, filename='Fg Snape_Office-1!', fandom='Harry P.')
    got = _parse_classification(json.dumps(dirty))
    assert got.filename == 'FgSnapeOffice1'
    assert got.fandom == 'HarryP'


def test_parse_digit_leading_prim_is_prefixed() -> None:
    dirty = dict(VERDICT, filename='42Wallpaper')
    assert _parse_classification(json.dumps(dirty)).filename == 'X42Wallpaper'


def test_parse_missing_fandom_falls_back_to_unknown() -> None:
    for absent in (dict(VERDICT, fandom=None), {'filename': 'PrWand'}):
        got = _parse_classification(json.dumps(absent))
        assert got.fandom == 'Unknown'


def test_parse_missing_filename_falls_back_to_item() -> None:
    got = _parse_classification(json.dumps(dict(VERDICT, filename=None)))
    assert got.filename == 'Item'


def test_spec_reads_the_gemini_env_names() -> None:
    spec = spec_from(
        {
            'GEMINI_API_KEY': 'k',
            'GEMINI_MODEL': 'm-classify',
            'GEMINI_BG_RESTORE_MODEL': 'm-restore',
        }
    )
    assert (spec.key, spec.model, spec.restore_model) == (
        'k',
        'm-classify',
        'm-restore',
    )


def test_spec_defaults() -> None:
    spec = spec_from({})
    assert spec.model == 'gemini-2.5-flash-lite'
    assert spec.restore_model == 'gemini-3-pro-image'


class _Reply:
    """A generate-content response double: .text may be set or raise."""

    def __init__(self, text: str | None, *, blocked: bool = False) -> None:
        self._text = text
        self._blocked = blocked

    @property
    def text(self) -> str | None:
        if self._blocked:
            raise ValueError('response has no candidates (safety)')
        return self._text


def test_text_of_extracts_and_strips() -> None:
    assert _text_of(_Reply('  hello  ')) == 'hello'
    assert _text_of(_Reply(None)) == ''


def test_text_of_blocked_response_is_llm_error() -> None:
    """The safety-blocked reply that used to crash sort now FAILS clean."""
    with pytest.raises(LlmError, match='no_text'):
        _text_of(_Reply(None, blocked=True))
