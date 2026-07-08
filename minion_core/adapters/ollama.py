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
import logging
import time
from typing import TYPE_CHECKING

from minion_core.adapters.llm import LlmError

if TYPE_CHECKING:
    from pathlib import Path

_LOG = logging.getLogger('llm')

CONNECT_TIMEOUT_SEC = 5
"""A model that is down must fail fast, not hang the whole pass."""

READ_TIMEOUT_SEC = 600
"""One inference may take minutes on a CPU NAS (bounded, BLUEPRINT 10)."""

HTTP_NOT_FOUND = 404
"""Ollama's status for a request naming a model it has not pulled."""


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
        _LOG.info('ollama prompt model=%s\n%s', self._model, prompt)
        message: dict[str, object] = {'role': 'user', 'content': prompt}
        if images:
            message['images'] = images
        payload: dict[str, object] = {
            'model': self._model,
            'messages': [message],
            'format': 'json',
            'stream': False,
        }
        return _content(self._send(payload))

    def _send(self, payload: dict[str, object]) -> dict[str, object]:
        """POST /api/chat; map every failure to a specific LlmError."""
        import requests

        # Announce the wait so a slow CPU run is not a silent gap.
        _LOG.info(
            'ollama awaiting model=%s (up to %ds)',
            self._model,
            READ_TIMEOUT_SEC,
        )
        started = time.monotonic()
        try:
            resp = requests.post(
                f'{self._url}/api/chat',
                json=payload,
                timeout=(CONNECT_TIMEOUT_SEC, READ_TIMEOUT_SEC),
            )
        except requests.RequestException as exc:
            raise _post_error(exc) from exc
        _LOG.info('ollama replied in %.0fs', time.monotonic() - started)
        if resp.status_code == HTTP_NOT_FOUND:
            # Server up, model absent -- the actionable one-time case.
            raise LlmError(f'model_not_pulled: run: ollama pull {self._model}')
        try:
            resp.raise_for_status()
            body = resp.json()
        except requests.RequestException as exc:
            raise LlmError(f'ollama_error: {exc}') from exc
        if not isinstance(body, dict):
            raise LlmError('ollama returned no object')
        return body


def _post_error(exc: Exception) -> LlmError:
    """Map a requests failure to a specific, actionable LlmError.

    A read timeout is NOT 'unreachable' -- the server answered, the
    model is just too slow on this CPU; say so and point at the fix.
    """
    import requests

    if isinstance(exc, requests.Timeout):
        return LlmError(
            f'ollama_timeout: the local model exceeded {READ_TIMEOUT_SEC}s -- '
            'too slow on this CPU; use a smaller OLLAMA_MODEL or switch to '
            'Gemini'
        )
    return LlmError(f'ollama_unreachable: {exc}')


def _content(body: dict[str, object]) -> str:
    """The assistant message text, or LlmError when absent."""
    message = body.get('message')
    text = message.get('content') if isinstance(message, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise LlmError('ollama returned no content')
    return text
