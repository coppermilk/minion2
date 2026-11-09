# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Serve one Step as a web service (http | mcp), picked by SKIN.

A minion's ``service.py`` calls ``run_service_app(step, make)``; ``make``
builds the one Step it serves, so the process imports only that minion --
never a catalog of every other service.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.core import Make


def run_service_app(step: str, make: Make) -> int:
    """Serve the Step over the SKIN facade: http (default) or mcp."""
    from minion_core.kernel import bot_logger

    # Log to stdout, which Synology Container Manager's Log tab reads; the
    # handler sits on root (bot_logger), so uvicorn's access lines and the
    # adapters' records (svc-restore's Gemini request/response under 'llm')
    # all land there too.
    bot_logger(step)
    skin = os.environ.get('SKIN', 'http')
    if skin == 'mcp':
        from services.mcp_server import create_server

        sys.stdout.write(f'service {step} starting (mcp)\n')
        sys.stdout.flush()
        create_server(step, make).run()
        return 0
    import uvicorn

    from services.http import create_app

    port = int(os.environ.get('PORT', '8000'))
    # A guaranteed, unbuffered first line so `docker logs` is never blank,
    # even if a logging handler is misconfigured. log_config=None hands
    # uvicorn's own access/error logs to our root stdout handler (from
    # bot_logger) instead of uvicorn's, so everything lands in one place.
    sys.stdout.write(f'service {step} starting on :{port}\n')
    sys.stdout.flush()
    uvicorn.run(
        create_app(step, make),
        # Localhost by default; the container opens all interfaces via
        # SERVICE_HOST=0.0.0.0 (compose), the host maps the port.
        host=os.environ.get('SERVICE_HOST', '127.0.0.1'),
        port=port,
        log_config=None,
    )
    return 0
