"""Phase 2 core: the data plane meets the Phase 0 dispatcher.

Hermetic (LocalStore, no web stack): a stored input is fetched, the Step
runs via invoke, and the output is put back byte-for-byte. This is the
logic both the HTTP and MCP skins call, so proving it here keeps the skin
tests thin (they live in services/tests, off the kernel gate).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.core import ServiceRequest
from services.core import run_service
from services.store import LocalStore

if TYPE_CHECKING:
    from pathlib import Path


def test_local_store_round_trips_bytes(tmp_path: Path) -> None:
    """Put then fetch returns the same bytes under a fresh name."""
    store = LocalStore(tmp_path / 'store')
    src = tmp_path / 'in' / 'a.jpg'
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b'img-bytes')
    ref = store.put('k/a.jpg', src)
    assert ref.startswith('file://')
    got = store.fetch(ref, tmp_path / 'work')
    assert got.read_bytes() == b'img-bytes'


def test_run_service_delivers_and_stores_output(tmp_path: Path) -> None:
    """Deliver over a stored input returns a delivered output ref."""
    store = LocalStore(tmp_path / 'store')
    src = tmp_path / 'in' / 'photo.jpg'
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b'hello')
    ref = store.put('inbox/photo.jpg', src)

    result = run_service(ServiceRequest('deliver', ref), store)

    assert result.disposition == 'delivered'
    assert result.output_ref is not None
    assert result.ms >= 0.0
    out = store.fetch(result.output_ref, tmp_path / 'out')
    assert out.read_bytes() == b'hello'  # bytes preserved end to end


def test_run_service_is_stateless(tmp_path: Path) -> None:
    """Two runs of the same input each deliver, independent of a tree."""
    store = LocalStore(tmp_path / 'store')
    src = tmp_path / 'in' / 'x.jpg'
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b'x')
    ref = store.put('inbox/x.jpg', src)
    first = run_service(ServiceRequest('deliver', ref), store)
    second = run_service(ServiceRequest('deliver', ref), store)
    assert first.disposition == second.disposition == 'delivered'
