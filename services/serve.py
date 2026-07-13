"""Serve one Step as a web service (http | mcp), picked by SKIN.

A minion's ``service.py`` calls ``run_service_app(step, make)``; ``make``
builds the one Step it serves, so the process imports only that minion --
never a catalog of every other service.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.core import Make

_LOG_DIR = Path('/logs')
"""Where a service writes its log file when the logs volume is mounted."""


def run_service_app(step: str, make: Make) -> int:
    """Serve the Step over the SKIN facade: http (default) or mcp."""
    from minion_core.kernel import bot_logger

    # Mirror to stdout AND /logs/<step>.log when the logs volume is mounted
    # (compose), so a service's records -- and svc-restore's Gemini
    # request/response under 'llm' -- are readable as a file too, not only
    # via docker logs. Both handlers sit on root (bot_logger), so uvicorn's
    # access lines and the adapters' records land in both sinks.
    bot_logger(step, _log_dir()).info('started')
    skin = os.environ.get('SKIN', 'http')
    if skin == 'mcp':
        from services.mcp_server import create_server

        create_server(step, make).run()
        return 0
    import uvicorn

    from services.http import create_app

    port = int(os.environ.get('PORT', '8000'))
    uvicorn.run(create_app(step, make), host='0.0.0.0', port=port)  # noqa: S104 -- bind all inside the container; expose per compose
    return 0


def _log_dir() -> Path | None:
    """The mounted logs dir, or None when it is not writable (local dev).

    A service is stateless, so its file sink is a small logs-only volume
    (compose), not /data. Where that volume is absent -- a bare local
    ``python -m ...service`` run -- there is nothing to create, so the
    service degrades to stdout-only rather than crashing on mkdir.
    """
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return _LOG_DIR
