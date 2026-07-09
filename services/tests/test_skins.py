"""Skin tests: HTTP (TestClient) and MCP (tool registration).

These need the web stack, so they live off the kernel gate; run with
`python -m pytest services/tests`. Behaviour is proved hermetically in
tests/test_service_core.py -- here we only check the two facades wire up
over the same core.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from services.http import create_app
from services.mcp_server import create_server
from services.store import LocalStore


def _stored(tmp_path: Path):
    store = LocalStore(tmp_path / 'store')
    src = tmp_path / 'in' / 'photo.jpg'
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b'hello')
    return store, store.put('inbox/photo.jpg', src)


def test_healthz(tmp_path: Path) -> None:
    store, _ = _stored(tmp_path)
    client = TestClient(create_app('deliver', store))
    reply = client.get('/healthz')
    assert reply.status_code == 200
    assert reply.json() == {'status': 'ok', 'step': 'deliver'}


def test_run_delivers_and_returns_ref(tmp_path: Path) -> None:
    store, ref = _stored(tmp_path)
    client = TestClient(create_app('deliver', store))
    reply = client.post('/run', json={'input_ref': ref})
    assert reply.status_code == 200
    body = reply.json()
    assert body['disposition'] == 'delivered'
    assert body['output_ref'].startswith('file://')
    assert body['ms'] >= 0.0


def test_openapi_exposes_run(tmp_path: Path) -> None:
    store, _ = _stored(tmp_path)
    client = TestClient(create_app('deliver', store))
    spec = client.get('/openapi.json').json()
    assert '/run' in spec['paths']  # n8n / orchestrator consume this


def test_mcp_registers_the_run_tool(tmp_path: Path) -> None:
    store, _ = _stored(tmp_path)
    server = create_server('deliver', store)
    tools = asyncio.run(server.list_tools())
    assert any(tool.name == 'run' for tool in tools)
