"""LLM boundary: image classification + background restore (google-genai).

Sole importer of the google SDK (REQ-ARC-002); loaded lazily so the
suite and non-LLM bots never touch it. Prompts come from
``minion_core.prompts`` -- one place per fact (BLUEPRINT 12).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol

from minion_core.adapters.files import usd_prim
from minion_core.kernel import Disposition
from minion_core.kernel import Step
from minion_core.kernel import Verdict
from minion_core.prompts import load_prompt
from minion_core.settings import UNKNOWN

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from minion_core.kernel import Job


class LlmError(Exception):
    """The model returned nothing usable."""


@dataclass(frozen=True)
class LlmSpec:
    """Endpoint identity: key + model ids (config, not code)."""

    key: str
    model: str
    restore_model: str


def spec_from(env: Mapping[str, str]) -> LlmSpec:
    """Build the spec from an explicitly passed mapping."""
    return LlmSpec(
        key=env.get('GEMINI_API_KEY', ''),
        model=env.get('GEMINI_MODEL', 'gemini-2.5-flash-lite'),
        restore_model=env.get('GEMINI_BG_RESTORE_MODEL', 'gemini-3-pro-image'),
    )


@dataclass(frozen=True)
class Classification:
    """The consumed slice of the model's JSON verdict (classify.md).

    The remaining JSON fields (character, layer, location, emotion)
    are prompt-side scaffolding: they shape ``filename`` but are not
    read by any bot.
    """

    fandom: str
    filename: str
    censored: bool
    confidence: str
    description: str


def _client(spec: LlmSpec) -> Any:  # noqa: ANN401 -- vendor client handle
    from google import genai

    return genai.Client(api_key=spec.key)


def _image_part(path: Path) -> Any:  # noqa: ANN401 -- vendor part handle
    from google.genai import types

    mime = 'image/png' if path.suffix.lower() == '.png' else 'image/jpeg'
    return types.Part.from_bytes(data=path.read_bytes(), mime_type=mime)


def _generate_text(model: str, contents: list[Any], spec: LlmSpec) -> str:
    """Generate and return the reply text; refusals become LlmError.

    Every remote failure (bad model id, bad key, quota, network) is
    an ``APIError`` subclass, mapped to ``LlmError`` here; text
    extraction (which raises on a safety block) is the pure,
    testable ``_text_of``. So the belt gets a stable FAILED with the
    real reason logged, never a ``step_crashed`` that hides it (the
    previous sort/restore failure).
    """
    from google.genai import errors

    try:
        response = _client(spec).models.generate_content(
            model=model, contents=contents
        )
    except errors.APIError as exc:
        raise LlmError(f'api_error: {exc}') from exc
    return _text_of(response)


class _TextResponse(Protocol):
    """The text accessor of a generate-content response.

    Reading ``text`` raises when the reply carries no usable
    candidate (a safety block) -- typed so ``_text_of`` needs no
    vendor ``Any`` and stays unit-testable without the SDK.
    """

    @property
    def text(self) -> str | None: ...


def _text_of(response: _TextResponse) -> str:
    """Reply text, or an LlmError when the model returned none."""
    try:
        return (response.text or '').strip()
    except (ValueError, AttributeError) as exc:
        raise LlmError(f'no_text: {exc}') from exc


def _generate_image(model: str, contents: list[Any], spec: LlmSpec) -> bytes:
    """Generate and return the first inline image; refusals -> LlmError."""
    from google.genai import errors

    try:
        response = _client(spec).models.generate_content(
            model=model, contents=contents
        )
    except errors.APIError as exc:
        raise LlmError(f'api_error: {exc}') from exc
    return _first_image(response)


def classify_image(path: Path, hint: str, spec: LlmSpec) -> Classification:
    """One JSON verdict per image: fandom, prim name, flags.

    ``hint`` is this week's script text (adapters.scripts); when
    present it rides into the prompt under the script_hint framing
    so scene labels land after the layer prefix.
    """
    prompt = load_prompt('classify')
    if hint:
        prompt = f'{prompt}\n\n{load_prompt("script_hint")}\n\n{hint}'
    text = _generate_text(spec.model, [prompt, _image_part(path)], spec)
    return _parse_classification(text)


def _parse_classification(text: str) -> Classification:
    """Coerce the raw reply into a Classification (pure, testable).

    The model is untrusted input (BLUEPRINT 4): fences are stripped,
    names are sanitized to prim identifiers, a missing fandom falls
    back to Unknown so the CLIP Re-place pass can rescue it later.
    """
    try:
        data = json.loads(_unfence(text))
    except ValueError as exc:
        raise LlmError(f'unparseable classify response: {exc}') from exc
    if not isinstance(data, dict):
        raise LlmError('classify response is not a JSON object')
    raw_fandom = _text_field(data, 'fandom')
    return Classification(
        fandom=usd_prim(raw_fandom) if raw_fandom else UNKNOWN,
        filename=usd_prim(_text_field(data, 'filename')),
        censored=bool(data.get('censored', False)),
        confidence=_text_field(data, 'confidence'),
        description=_text_field(data, 'description'),
    )


def _text_field(data: dict[str, Any], key: str) -> str:
    """A string field of the reply; null and absence read as ''."""
    value = data.get(key)
    return value.strip() if isinstance(value, str) else ''


def _unfence(text: str) -> str:
    """Strip an optional markdown code fence around the JSON."""
    body = text.strip()
    if body.startswith('```'):
        body = body.partition('\n')[2]
        body = body.rpartition('```')[0]
    return body.strip()


def restore_background(path: Path, spec: LlmSpec) -> Path:
    """Repaint hidden regions; writes the ``_s2`` sibling file."""
    contents = [load_prompt('restore_background'), _image_part(path)]
    data = _generate_image(spec.restore_model, contents, spec)
    out = path.with_stem(path.stem.removesuffix('_s1') + '_s2')
    out.write_bytes(data)
    return out


def _first_image(response: Any) -> bytes:  # noqa: ANN401 -- vendor response
    for cand in response.candidates or []:
        for part in cand.content.parts or []:
            blob = getattr(part, 'inline_data', None)
            if blob is not None and blob.data:
                return bytes(blob.data)
    raise LlmError('no image in restore response')


class RestoreBackground(Step):
    """Repaint the hidden regions of a censored image.

    The second step of the restore bot's two-step belt: consumes the
    ``_s1`` file HidePeople produced, delivers the ``_s2`` repaint
    (OPERATIONS 6 naming).
    """

    def __init__(self, spec: LlmSpec) -> None:
        self._spec = spec

    def process(self, job: Job) -> Verdict:
        """Repaint; a refused model is a stable failure code."""
        try:
            out = restore_background(job.src, self._spec)
        except LlmError:
            return Verdict(Disposition.FAILED, reason='restore_failed')
        return Verdict(Disposition.DELIVERED, result=out, reply='restored')
