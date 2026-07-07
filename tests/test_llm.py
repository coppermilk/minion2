"""llm adapter tests: JSON verdict parsing and model spec wiring."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from minion_core.adapters.llm import LlmError
from minion_core.adapters.llm import _parse_classification
from minion_core.adapters.llm import _text_of
from minion_core.adapters.llm import spec_from

if TYPE_CHECKING:
    from pathlib import Path

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


def test_spec_thinking_budget() -> None:
    """Reasoning is on (dynamic) by default; the budget is a knob."""
    assert spec_from({}).thinking_budget == -1
    assert spec_from({'GEMINI_THINKING_BUDGET': '512'}).thinking_budget == 512
    assert spec_from({'GEMINI_THINKING_BUDGET': '0'}).thinking_budget == 0


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


class _FakeBackend:
    """A Backend double: returns a canned reply, records the calls."""

    name = 'fake'

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.seen: list[tuple[str, ...]] = []

    def vision_json(self, prompt: str, image: object) -> str:
        self.seen.append(('vision', prompt, str(image)))
        return self._reply

    def text(self, prompt: str) -> str:
        self.seen.append(('text', prompt))
        return self._reply


def test_classify_image_uses_the_backend(tmp_path: Path) -> None:
    """classify_image is vendor-blind: it just calls vision_json."""
    from minion_core.adapters.llm import classify_image

    img = tmp_path / 'x.jpg'
    img.write_bytes(b'x')
    backend = _FakeBackend(json.dumps(VERDICT))
    got = classify_image(img, '', backend)
    assert got.filename == 'FgSnapeOfficeAngry'
    assert backend.seen[0][0] == 'vision'


def test_classify_image_folds_in_the_hint(tmp_path: Path) -> None:
    """The weekly hint rides into the prompt when present."""
    from minion_core.adapters.llm import classify_image

    img = tmp_path / 'x.jpg'
    img.write_bytes(b'x')
    backend = _FakeBackend(json.dumps(VERDICT))
    classify_image(img, 'WEEKTEXT', backend)
    assert 'WEEKTEXT' in backend.seen[0][1]


def test_list_props_parses_object_and_list() -> None:
    from minion_core.adapters.llm import list_props

    obj = _FakeBackend('{"props": ["Wand", "Bag"]}')
    assert list_props('s', obj) == ['Wand', 'Bag']
    bare = _FakeBackend('["Cup", "Hat"]')
    assert list_props('s', bare) == ['Cup', 'Hat']


def test_list_props_garbage_raises() -> None:
    from minion_core.adapters.llm import list_props

    with pytest.raises(LlmError):
        list_props('s', _FakeBackend('the model rambled'))
    with pytest.raises(LlmError):
        list_props('s', _FakeBackend('{"nope": 1}'))
