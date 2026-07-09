"""Phase 5: the platform API serves the canvas as static assets.

Services tier. The browser-driven check (build a pipeline, run it, watch
the nodes light up) is done with Playwright out of band; here we only
assert the static UI is mounted and reachable, so the gate stays fast.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from services.api import create_api
from services.repo import InMemoryRepo
from services.store import LocalStore


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_api(InMemoryRepo(), LocalStore(tmp_path / 's')))


def test_ui_index_is_served(tmp_path: Path) -> None:
    reply = _client(tmp_path).get('/ui/')
    assert reply.status_code == 200
    assert 'minion canvas' in reply.text


def test_ui_app_js_is_served(tmp_path: Path) -> None:
    reply = _client(tmp_path).get('/ui/app.js')
    assert reply.status_code == 200
    assert 'streamEvents' in reply.text  # the SSE animation loop
