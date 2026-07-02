"""LLM boundary: image naming + background restore (google-genai).

Sole importer of the google SDK (REQ-ARC-002); loaded lazily so the
suite and non-LLM bots never touch it. Prompts come from
``minion_core.prompts`` -- one place per fact (BLUEPRINT 12).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from minion_core.adapters.files import sanitize
from minion_core.kernel import Disposition
from minion_core.kernel import Step
from minion_core.kernel import Verdict
from minion_core.prompts import load_prompt

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
    name_model: str
    image_model: str


def spec_from(env: Mapping[str, str]) -> LlmSpec:
    """Build the spec from an explicitly passed mapping."""
    return LlmSpec(
        key=env.get('GEMINI_API_KEY', ''),
        name_model=env.get('LLM_NAME_MODEL', 'gemini-2.5-flash'),
        image_model=env.get('LLM_IMAGE_MODEL', 'gemini-2.5-flash-image'),
    )


def _client(spec: LlmSpec) -> Any:  # noqa: ANN401 -- vendor client handle
    from google import genai

    return genai.Client(api_key=spec.key)


def _image_part(path: Path) -> Any:  # noqa: ANN401 -- vendor part handle
    from google.genai import types

    mime = 'image/png' if path.suffix.lower() == '.png' else 'image/jpeg'
    return types.Part.from_bytes(data=path.read_bytes(), mime_type=mime)


def name_image(path: Path, spec: LlmSpec) -> str:
    """A short, safe label for the image (sort pass 1)."""
    response = _client(spec).models.generate_content(
        model=spec.name_model,
        contents=[load_prompt('name_image'), _image_part(path)],
    )
    text = (response.text or '').strip()
    if not text:
        raise LlmError('empty naming response')
    return sanitize(text.splitlines()[0])


def restore_background(path: Path, spec: LlmSpec) -> Path:
    """Repaint hidden regions; writes the ``_s2`` sibling file."""
    response = _client(spec).models.generate_content(
        model=spec.image_model,
        contents=[load_prompt('restore_background'), _image_part(path)],
    )
    data = _first_image(response)
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
