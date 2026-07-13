"""frames web service: a video (or link) -> a zip of frames, bytes in/out.

    python -m minions.svc.frames.service   (SKIN=http | mcp)

Owns the extraction Step; the link download that can precede it is the shared
yt-dlp adapter. The full pipeline (fetch -> extract) is the chain served here.
Knows only itself -- no catalog, no sibling service, no Telegram.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from minion_core.adapters.fetch import FetchLink
from minions.svc.frames.step import ExtractFrames
from services.serve import run_service_app

if TYPE_CHECKING:
    from minion_core.kernel import Stage
    from minion_core.settings import Settings


def _make(cfg: Settings) -> Stage:
    """Build the frames chain: fetch a link (or pass a file), then extract."""
    return FetchLink(cfg) >> ExtractFrames(cfg)


def main() -> int:
    """Serve the frames chain over the SKIN facade."""
    code: int = run_service_app('frames', _make)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
