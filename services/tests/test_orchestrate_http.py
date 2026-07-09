"""The HTTP transport for the orchestrator (services tier, off the gate).

HttpCaller POSTs to a Step service's /run. Here a FastAPI TestClient (an
httpx.Client) stands in for the network, proving the orchestrator drives a
real HTTP round-trip. The multi-step walk itself is proved hermetically in
tests/test_orchestrate.py with LocalCaller.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from services.http import create_app
from services.orchestrate import HttpCaller
from services.orchestrate import RunRequest
from services.orchestrate import run_graph
from services.store import LocalStore


def test_run_graph_over_http(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / 'store')
    src = tmp_path / 'in' / 'photo.jpg'
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b'hello')
    ref = store.put('inbox/photo.jpg', src)

    client = TestClient(create_app('deliver', store))
    caller = HttpCaller(lambda _step: '', client)
    spec = {
        'bot': 'demo',
        'stages': [
            {'source': 'folder', 'root': 'bot_dir', 'exts': ['.jpg']},
            {'step': 'deliver'},
        ],
    }

    result = run_graph(RunRequest(spec, ref), caller)
    assert [u.step for u in result.usage] == ['deliver']
    assert result.usage[0].disposition == 'delivered'
    assert result.final_ref is not None
    assert result.final_ref.startswith('file://')
