"""fetch web service: a link -> a video, bytes in / bytes out.

    python -m minions.svc.fetch.service   (SKIN=http | mcp)

The input is a ``.url`` file (the link); FetchLink downloads the video and
passes a media file through untouched. Knows only itself -- no catalog, no
sibling service, no Telegram.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from minion_core.adapters.fetch import FetchLink
from services.serve import run_service_app

if TYPE_CHECKING:
    from minion_core.kernel import Stage
    from minion_core.settings import Settings


def _make(cfg: Settings) -> Stage:
    """Build the link-fetch Step."""
    return FetchLink(cfg)


def main() -> int:
    """Serve the link-fetch Step over the SKIN facade."""
    code: int = run_service_app('fetch', _make)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
