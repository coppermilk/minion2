"""Phase 4 platform API: catalog, tenant-scoped graphs, runs, events, usage.

Services tier (needs the web stack), off the kernel gate. The API drives
the Phase 3 orchestrator; behaviour of the orchestrator/core is proved
hermetically elsewhere, so here we check the multi-tenant surface: scoping,
a run round-trip, the SSE event replay, and the usage/RU aggregation.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from services.api import create_api
from services.repo import InMemoryRepo
from services.store import LocalStore

H1 = {'X-Tenant-Id': 't1'}
H2 = {'X-Tenant-Id': 't2'}

SPEC = {
    'bot': 'demo',
    'stages': [
        {'source': 'folder', 'root': 'bot_dir', 'exts': ['.jpg']},
        {'step': 'deliver'},
    ],
}


def _client(tmp_path: Path):
    store = LocalStore(tmp_path / 'store')
    return TestClient(create_api(InMemoryRepo(), store)), store


def _seed(store: LocalStore, tmp_path: Path) -> str:
    src = tmp_path / 'in' / 'photo.jpg'
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b'hello')
    return store.put('inbox/photo.jpg', src)


def test_catalog_lists_the_palette(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    body = client.get('/catalog', headers=H1).json()
    assert 'deliver' in body['steps']
    assert 'folder' in body['sources']
    assert 'reply' in body['sinks']


def test_missing_tenant_is_rejected(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    assert client.get('/graphs').status_code == 422  # header required


def test_graphs_are_tenant_scoped(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    graph = client.post(
        '/graphs', headers=H1, json={'name': 'demo', 'spec': SPEC}
    ).json()
    assert graph['tenant_id'] == 't1'
    # another tenant cannot see it, the owner can
    assert client.get(f'/graphs/{graph["id"]}', headers=H2).status_code == 404
    assert client.get(f'/graphs/{graph["id"]}', headers=H1).status_code == 200
    assert len(client.get('/graphs', headers=H1).json()) == 1
    assert client.get('/graphs', headers=H2).json() == []


def test_run_streams_live_then_completes(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    ref = _seed(store, tmp_path)
    graph = client.post(
        '/graphs', headers=H1, json={'name': 'demo', 'spec': SPEC}
    ).json()

    # runs are async now: POST returns 'running' immediately
    started = client.post(
        '/runs',
        headers=H1,
        json={'graph_id': graph['id'], 'input_ref': ref},
    ).json()
    assert started['status'] == 'running'

    # draining the live SSE blocks until the run closes
    stream = client.get(f'/runs/{started["id"]}/events', headers=H1).text
    assert 'step:deliver#1' in stream
    assert 'left' in stream

    done = client.get(f'/runs/{started["id"]}', headers=H1).json()
    assert done['status'] == 'done'
    assert done['final_ref'].startswith('file://')

    usage = client.get('/usage', headers=H1).json()
    assert usage['nodes'] == 1.0
