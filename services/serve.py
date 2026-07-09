"""Service entrypoint: pick the skin by env and serve one Step.

    STEP=frames SKIN=http python -m services.serve   # uvicorn + OpenAPI
    STEP=frames SKIN=mcp  python -m services.serve   # MCP (stdio)

One image, N containers: STEP selects the Step, SKIN the facade, PORT the
HTTP port. Kept tiny -- the logic is in core.py; this only starts a server.
"""

from __future__ import annotations

import os


def _serve_mcp(step: str) -> None:
    from services.mcp_server import create_server
    from services.mcp_server import store_from_env

    create_server(step, store_from_env()).run()


def _serve_http(step: str) -> None:
    import uvicorn

    from services.http import create_app
    from services.http import store_from_env

    port = int(os.environ.get('PORT', '8000'))
    uvicorn.run(create_app(step, store_from_env()), host='0.0.0.0', port=port)  # noqa: S104 -- bind all inside the container; expose per compose


def main() -> int:
    """Serve the configured Step over the configured skin."""
    step = os.environ.get('STEP', 'deliver')
    if os.environ.get('SKIN', 'http') == 'mcp':
        _serve_mcp(step)
    else:
        _serve_http(step)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
