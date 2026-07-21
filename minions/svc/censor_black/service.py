"""censor-black web service: black out faces, bytes in / bytes out.

    python -m minions.svc.censor_black.service   (SKIN=http | mcp)

Knows only itself: imports its own Step and the shared serving framework --
no catalog, no sibling service, no Telegram.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from minions.svc.censor_black.step import HideFaces
from services.serve import run_service_app

if TYPE_CHECKING:
    from minion_core.kernel import Stage
    from minion_core.settings import Settings


def _make(_cfg: Settings) -> Stage:
    """Build the face-blackout Step (no config needed)."""
    return HideFaces()


def main() -> int:
    """Serve the face-blackout Step over the SKIN facade."""
    code: int = run_service_app('censor-black', _make)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
