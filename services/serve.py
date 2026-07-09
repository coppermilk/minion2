"""Service entrypoint: pick the skin by env and serve.

    STEP=frames SKIN=http python -m services.serve   # uvicorn + OpenAPI
    STEP=frames SKIN=mcp  python -m services.serve   # MCP (stdio)
    SKIN=api              python -m services.serve   # platform API

One image, N containers: STEP selects the Step, SKIN the facade, PORT the
HTTP port. Kept tiny -- the logic is elsewhere; this only starts a server.
"""

from __future__ import annotations

import os


def _serve_mcp(step: str) -> None:
    from services.mcp_server import create_server
    from services.store import store_from_env

    create_server(step, store_from_env()).run()


def _serve_asgi(target: str) -> None:
    import uvicorn

    port = int(os.environ.get('PORT', '8000'))
    uvicorn.run(target, host='0.0.0.0', port=port)  # noqa: S104 -- bind all inside the container; expose per compose


def main() -> int:
    """Serve the configured skin: a Step (http/mcp) or the platform api."""
    skin = os.environ.get('SKIN', 'http')
    if skin == 'mcp':
        _serve_mcp(os.environ.get('STEP', 'deliver'))
    elif skin == 'api':
        _serve_asgi('services.api:app')
    else:
        _serve_asgi('services.http:app')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
