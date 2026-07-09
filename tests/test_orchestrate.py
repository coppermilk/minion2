"""Phase 3 orchestrator (Mode B): walk a graph as service calls.

Hermetic (LocalCaller + LocalStore, no web stack): the orchestrator runs a
two-step chain (fetch -> deliver), threading each output ref into the next
input, emits the Phase 1.5 events, and records per-node Usage. The HTTP
transport is exercised in services/tests, off the kernel gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from minion_core.events import Collector
from services.orchestrate import LocalCaller
from services.orchestrate import RunRequest
from services.orchestrate import run_graph
from services.orchestrate import steps_of
from services.store import LocalStore

if TYPE_CHECKING:
    from pathlib import Path


def _spec() -> dict:
    return {
        'bot': 'demo',
        'stages': [
            {'source': 'folder', 'root': 'bot_dir', 'exts': ['.jpg']},
            {'step': 'fetch'},
            {'step': 'deliver'},
        ],
    }


def _seed(store: LocalStore, tmp_path: Path) -> str:
    src = tmp_path / 'in' / 'photo.jpg'
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b'hello')
    return store.put('inbox/photo.jpg', src)


def test_steps_of_orders_the_step_nodes() -> None:
    """Only step nodes, in order, keyed by their assigned ids."""
    assert steps_of(_spec()) == [
        ('step:fetch#1', 'fetch'),
        ('step:deliver#2', 'deliver'),
    ]


def test_run_graph_threads_refs_through_the_chain(tmp_path: Path) -> None:
    """Each output ref feeds the next Step; the bytes survive end to end."""
    store = LocalStore(tmp_path / 'store')
    ref = _seed(store, tmp_path)
    result = run_graph(RunRequest(_spec(), ref), LocalCaller(store))
    assert [u.step for u in result.usage] == ['fetch', 'deliver']
    assert all(u.disposition == 'delivered' for u in result.usage)
    assert result.final_ref is not None
    out = store.fetch(result.final_ref, tmp_path / 'out')
    assert out.read_bytes() == b'hello'


def test_run_graph_emits_events_per_node(tmp_path: Path) -> None:
    """Mode B emits the same entered/left events as the Mode A taps."""
    store = LocalStore(tmp_path / 'store')
    ref = _seed(store, tmp_path)
    col = Collector()
    run_graph(RunRequest(_spec(), ref), LocalCaller(store), col)
    trace = [(e.node, e.phase) for e in col.events]
    assert trace == [
        ('step:fetch#1', 'entered'),
        ('step:fetch#1', 'left'),
        ('step:deliver#2', 'entered'),
        ('step:deliver#2', 'left'),
    ]
    assert col.events[-1].disposition == 'delivered'
