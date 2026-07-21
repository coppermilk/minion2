"""MCP skin over the service core.

The same Step exposed as an MCP tool, so agents (Claude, ...) call it just
like the HTTP skin: one core, two facades. Parameterized by STEP -- the
detach guarantee at the protocol level (an agent reaches the same Step, and
the IP never leaves our code). The tool takes a local file path and returns
the result path; a fresh ephemeral store backs each call (no shared store).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP

from services.core import ServiceRequest
from services.core import run_service
from services.store import LocalStore

if TYPE_CHECKING:
    from services.core import Make


def create_server(step: str, make: Make) -> FastMCP:
    """Build the one-Step MCP server exposing a ``run`` tool."""
    server = FastMCP(f'service-{step}')

    @server.tool()
    def run(input_path: str) -> dict[str, object]:
        """Run the Step over a local file; return the output path + verdict."""
        work = Path(tempfile.mkdtemp(prefix='svc-mcp-'))
        store = LocalStore(work / 'store')
        src = Path(input_path)
        ref = store.put(src.name, src)
        result = run_service(ServiceRequest(step, ref), store, make)
        out = (
            str(store.fetch(result.output_ref, work / 'out'))
            if result.output_ref is not None
            else None
        )
        return {
            'output_path': out,
            'outputs': result.outputs,
            'disposition': result.disposition,
            'reason': result.reason,
            'ms': result.ms,
        }

    return server
