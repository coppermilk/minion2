"""Phase 6: Resource Units and the period-aware billing endpoint.

Services tier. The RU math is pure; the endpoint aggregates a tenant's
usage over an optional [since, until] window and reports the per-request
time on the run.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from services.api import create_api
from services.billing import DEFAULT_TARIFF
from services.billing import resource_units
from services.models import UsageRecord
from services.repo import InMemoryRepo
from services.store import LocalStore

H1 = {'X-Tenant-Id': 't1'}
SPEC = {
    'bot': 'demo',
    'stages': [
        {'source': 'folder', 'root': 'bot_dir', 'exts': ['.jpg']},
        {'step': 'deliver'},
    ],
}


def _rec(ms: float, ts: float) -> UsageRecord:
    return UsageRecord(
        id='x',
        tenant_id='t1',
        run_id='r',
        node='n',
        step='deliver',
        disposition='delivered',
        ms=ms,
        ts=ts,
    )


def test_resource_units_compute_from_ms() -> None:
    """Compute RU = vCPU-hours x c_cpu; other dimensions are zero."""
    units = resource_units([_rec(3600_000.0, 0.0)], DEFAULT_TARIFF)
    assert units.compute == DEFAULT_TARIFF.c_cpu  # 1 hour of one vCPU
    assert units.total == units.compute
    assert units.memory == 0.0


def _client(tmp_path: Path) -> tuple[TestClient, LocalStore]:
    store = LocalStore(tmp_path / 'store')
    return TestClient(create_api(InMemoryRepo(), store)), store


def _seed(store: LocalStore, tmp_path: Path) -> str:
    src = tmp_path / 'in' / 'photo.jpg'
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b'hello')
    return store.put('inbox/photo.jpg', src)


def test_run_reports_total_ms(tmp_path: Path) -> None:
    """The run carries its own processing time (time to the user)."""
    client, store = _client(tmp_path)
    ref = _seed(store, tmp_path)
    graph = client.post(
        '/graphs', headers=H1, json={'name': 'd', 'spec': SPEC}
    ).json()
    run = client.post(
        '/runs', headers=H1, json={'graph_id': graph['id'], 'input_ref': ref}
    ).json()
    assert run['total_ms'] >= 0.0


def test_billing_aggregates_and_windows(tmp_path: Path) -> None:
    """Billing sums a tenant's RU and respects a future 'since'."""
    client, store = _client(tmp_path)
    ref = _seed(store, tmp_path)
    graph = client.post(
        '/graphs', headers=H1, json={'name': 'd', 'spec': SPEC}
    ).json()
    client.post(
        '/runs', headers=H1, json={'graph_id': graph['id'], 'input_ref': ref}
    )
    bill = client.get('/billing', headers=H1).json()
    assert bill['nodes'] == 1.0
    assert bill['total'] == bill['compute'] >= 0.0
    # a window starting far in the future excludes everything
    future = client.get('/billing?since=9999999999', headers=H1).json()
    assert future['nodes'] == 0.0
    assert future['total'] == 0.0
