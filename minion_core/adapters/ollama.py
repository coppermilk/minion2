"""Local model boundary: Ollama HTTP (Qwen2.5-VL on the NAS).

Third sanctioned ``requests`` import site (REQ-ARC-002): the local
model runs in the sibling ``ollama`` container, reached over the
compose network. Implements the ``Backend`` contract (adapters.llm),
so classify/props stay vendor-blind. Every failure maps to
``LlmError`` -- a model that is down leaves the image waiting in
``_inbox`` (the belt's punt), never a crash.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from minion_core.adapters.llm import LlmError

if TYPE_CHECKING:
    from pathlib import Path

CONNECT_TIMEOUT_SEC = 5
"""A model that is down must fail fast, not hang the whole pass."""

READ_TIMEOUT_SEC = 600
"""One inference may take minutes on a CPU NAS (bounded, BLUEPRINT 10)."""


class OllamaBackend:
    """Backend over a local Ollama server -- the default offline path."""

    name = 'local'

    def __init__(self, url: str, model: str) -> None:
        self._url = url.rstrip('/')
        self._model = model

    def vision_json(self, prompt: str, image: Path) -> str:
        """Classify one image; an unreachable model -> LlmError."""
        img = base64.b64encode(image.read_bytes()).decode('ascii')
        return self._chat(prompt, [img])

    def text(self, prompt: str) -> str:
        """Text-only completion; an unreachable model -> LlmError."""
        return self._chat(prompt, None)

    def _chat(self, prompt: str, images: list[str] | None) -> str:
        """One /api/chat round-trip; JSON-forced, non-streaming."""
        import requests

        message: dict[str, object] = {'role': 'user', 'content': prompt}
        if images:
            message['images'] = images
        payload: dict[str, object] = {
            'model': self._model,
            'messages': [message],
            'format': 'json',
            'stream': False,
        }
        try:
            resp = requests.post(
                f'{self._url}/api/chat',
                json=payload,
                timeout=(CONNECT_TIMEOUT_SEC, READ_TIMEOUT_SEC),
            )
            resp.raise_for_status()
            body = resp.json()
        except requests.RequestException as exc:
            raise LlmError(f'ollama_unreachable: {exc}') from exc
        content = body.get('message', {}).get('content', '')
        if not isinstance(content, str) or not content.strip():
            raise LlmError('ollama returned no content')
        return content
