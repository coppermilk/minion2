"""restore web service: blur people + LLM-repaint the scene, bytes in/out.

    python -m minions.svc.restore.service   (SKIN=http | mcp)

Owns its mark Step; the repaint is the shared Gemini adapter. The full
pipeline (mark -> repaint) is the chain served here. Needs GEMINI_API_KEY in
the environment. Knows only itself -- no catalog, no sibling, no Telegram.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from minion_core.adapters.llm import RestoreBackground
from minion_core.adapters.llm import spec_from
from minions.svc.restore.step import HidePersonBoxes
from services.serve import run_service_app

if TYPE_CHECKING:
    from minion_core.kernel import Stage
    from minion_core.settings import Settings


def _make(_cfg: Settings) -> Stage:
    """Build the restore chain: mark people, then LLM-repaint the scene."""
    return HidePersonBoxes() >> RestoreBackground(spec_from(os.environ))


def main() -> int:
    """Serve the restore chain over the SKIN facade."""
    code: int = run_service_app('restore', _make)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
