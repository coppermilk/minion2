"""Skin tests: HTTP (TestClient) and MCP (tool registration).

These need the web stack, so they live off the kernel gate; run with
`python -m pytest services/tests`. Behaviour is proved hermetically in
tests/test_service_core.py -- here we only check the two facades wire up
over the same core (bytes in, bytes out; no shared store).
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from services.http import create_app
from services.mcp_server import create_server


def test_healthz() -> None:
    client = TestClient(create_app('deliver'))
    reply = client.get('/healthz')
    assert reply.status_code == 200
    assert reply.json() == {'status': 'ok', 'step': 'deliver'}


def test_run_file_delivers_the_bytes() -> None:
    client = TestClient(create_app('deliver'))
    reply = client.post(
        '/run-file',
        files={'file': ('a.bin', b'hello', 'application/octet-stream')},
    )
    assert reply.status_code == 200
    assert reply.content == b'hello'  # deliver just relocates the bytes


def test_openapi_exposes_run_file() -> None:
    client = TestClient(create_app('deliver'))
    spec = client.get('/openapi.json').json()
    assert '/run-file' in spec['paths']  # n8n / relays consume this


def test_mcp_registers_the_run_tool() -> None:
    server = create_server('deliver')
    tools = asyncio.run(server.list_tools())
    assert any(tool.name == 'run' for tool in tools)
