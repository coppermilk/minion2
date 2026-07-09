"""MCP skin over the service core (PLATFORM.md, section 3).

The same Step exposed as an MCP tool, so agents (Claude, ...) call it just
like the HTTP skin: one core, two facades. Parameterized by STEP -- the
detach guarantee at the protocol level (an agent or n8n reaches the same
Step, and the IP never leaves our code).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP

from services.core import ServiceRequest
from services.core import run_service

if TYPE_CHECKING:
    from services.store import Store


def create_server(step: str, store: Store) -> FastMCP:
    """Build the one-Step MCP server exposing a ``run`` tool."""
    server = FastMCP(f'service-{step}')

    @server.tool()
    def run(input_ref: str) -> dict[str, object]:
        """Run the Step over a stored input; return output ref + verdict."""
        result = run_service(ServiceRequest(step, input_ref), store)
        return {
            'output_ref': result.output_ref,
            'disposition': result.disposition,
            'reason': result.reason,
            'ms': result.ms,
        }

    return server
