"""Model backend selection: local Qwen (Ollama) or Gemini.

The switch bot flips a one-word toggle in STATE; every model-backed
bot (classify, props) resolves the live backend here, so a switch
takes effect on the next item with no restart. Restore is NOT routed
through here -- image generation stays Gemini-only.

Neither branch pulls a vendor at import time: ``GeminiBackend`` and
``OllamaBackend`` lazy-import google/requests inside their calls, so
importing this module stays cheap for non-model code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from minion_core.adapters.llm import GeminiBackend
from minion_core.adapters.llm import spec_from
from minion_core.adapters.ollama import OllamaBackend
from minion_core.kernel import atomic_write

if TYPE_CHECKING:
    from collections.abc import Mapping

    from minion_core.adapters.llm import Backend
    from minion_core.settings import Settings

LOCAL = 'local'
"""Toggle value: the local Qwen (Ollama) backend -- the default."""

GEMINI = 'gemini'
"""Toggle value: the Gemini cloud backend."""

CHOICES = (LOCAL, GEMINI)
"""The only accepted toggle values."""


class BackendToggle:
    """The active-backend name on disk (STATE; survives restarts).

    An absent or unrecognised value reads as the configured default
    (``MODEL_BACKEND``, itself defaulting to ``local``) so a fresh
    deploy is offline-first with no setup.
    """

    def __init__(self, cfg: Settings) -> None:
        self._path = cfg.state / 'model.backend'
        self._default = (
            cfg.model_backend if cfg.model_backend in CHOICES else LOCAL
        )

    def read(self) -> str:
        """The stored backend name, or the default when unset."""
        try:
            name = self._path.read_text(encoding='ascii').strip()
        except OSError:
            return self._default
        return name if name in CHOICES else self._default

    def write(self, name: str) -> str:
        """Persist a choice atomically (REQ-DATA-002); returns it."""
        if name not in CHOICES:
            raise ValueError(name)
        atomic_write(self._path, name.encode('ascii'))
        return name


def select_backend(cfg: Settings, env: Mapping[str, str]) -> Backend:
    """The live backend per the toggle (default from MODEL_BACKEND)."""
    if BackendToggle(cfg).read() == GEMINI:
        return GeminiBackend(spec_from(env))
    return OllamaBackend(cfg.ollama_url, cfg.ollama_model)
