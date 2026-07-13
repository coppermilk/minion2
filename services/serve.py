"""Serve one Step as a web service (http | mcp), picked by SKIN.

A minion's ``service.py`` calls ``run_service_app(step, make)``; ``make``
builds the one Step it serves, so the process imports only that minion --
never a catalog of every other service.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.core import Make


def run_service_app(step: str, make: Make) -> int:
    """Serve the Step over the SKIN facade: http (default) or mcp."""
    from minion_core.kernel import bot_logger

    # A service is stateless (no /data), so stdout is its only log sink:
    # put the mirror on root so the Step's own records and the vendor
    # adapters' (Gemini request/response under 'llm', for svc-restore)
    # reach docker logs, not just uvicorn's access lines.
    bot_logger(step, None).info('started')
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
