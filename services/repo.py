"""In-memory tenant-scoped repository (PLATFORM.md, section 7).

The schema is defined now; the backend is pluggable, as with the Store. A
Postgres/SQLAlchemy impl is a follow-up -- this in-memory one keeps the API
hermetically testable and every access tenant-scoped. Thread-safe for the
API server.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from minion_core.events import Event
    from services.models import Graph
    from services.models import Run
    from services.models import UsageRecord


class InMemoryRepo:
    """Tenant-scoped stores for graphs, runs, usage, and run events."""

    def __init__(self) -> None:
        self._graphs: dict[str, Graph] = {}
        self._runs: dict[str, Run] = {}
        self._usage: list[UsageRecord] = []
        self._events: dict[str, list[Event]] = {}
        self._lock = threading.Lock()

    def add_graph(self, graph: Graph) -> None:
        """Store a graph."""
        with self._lock:
            self._graphs[graph.id] = graph

    def get_graph(self, tenant: str, graph_id: str) -> Graph | None:
        """The tenant's graph by id, or None if absent/foreign."""
        with self._lock:
            graph = self._graphs.get(graph_id)
        if graph is None or graph.tenant_id != tenant:
            return None
        return graph

    def list_graphs(self, tenant: str) -> list[Graph]:
        """The tenant's graphs."""
        with self._lock:
            return [g for g in self._graphs.values() if g.tenant_id == tenant]

    def add_run(self, run: Run) -> None:
        """Store a run."""
        with self._lock:
            self._runs[run.id] = run

    def get_run(self, tenant: str, run_id: str) -> Run | None:
        """The tenant's run by id, or None if absent/foreign."""
        with self._lock:
            run = self._runs.get(run_id)
        if run is None or run.tenant_id != tenant:
            return None
        return run

    def add_usage(self, record: UsageRecord) -> None:
        """Append one usage record."""
        with self._lock:
            self._usage.append(record)

    def list_usage(self, tenant: str) -> list[UsageRecord]:
        """The tenant's usage records."""
        with self._lock:
            return [u for u in self._usage if u.tenant_id == tenant]

    def set_events(self, run_id: str, events: list[Event]) -> None:
        """Store the events a run emitted (for SSE replay)."""
        with self._lock:
            self._events[run_id] = list(events)

    def list_events(self, run_id: str) -> list[Event]:
        """The events a run emitted."""
        with self._lock:
            return list(self._events.get(run_id, []))
