"""Multi-tenant platform API (PLATFORM.md, section 7).

FastAPI over the Phase 3 orchestrator: a tenant stores graphs, starts runs,
and reads back the flow (SSE) and usage (RU). The tenant comes from a header
now (``X-Tenant-Id``); real auth (OIDC) is a progressive follow-up. Every
access is tenant-scoped. ``/catalog`` is the palette a React Flow canvas
builds nodes from (Phase 5).

Runs execute in the background and their events stream live over SSE (a
run publishes into an in-memory RunBus as it happens; a durable bus is the
cloud swap).
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Annotated

from fastapi import Depends
from fastapi import FastAPI
from fastapi import Header
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from minion_core.graph import SINKS
from minion_core.graph import SOURCES
from minions.service import CATALOG
from services.billing import DEFAULT_TARIFF
from services.billing import Tariff
from services.billing import resource_units
from services.bus import RunBus
from services.models import Graph
from services.models import GraphCreate
from services.models import Run
from services.models import RunStart
from services.models import UsageRecord
from services.orchestrate import LocalCaller
from services.orchestrate import RunRequest
from services.orchestrate import run_graph
from services.repo import InMemoryRepo
from services.store import LocalStore

if TYPE_CHECKING:
    from collections.abc import Iterable
    from collections.abc import Iterator

    from minion_core.events import Emit
    from minion_core.events import Event
    from services.orchestrate import Usage
    from services.store import Store

C_CPU = 0.05
"""Internal Compute tariff (RU per vCPU-hour); decoupled from public price."""


@dataclass(frozen=True)
class Deps:
    """The server's dependencies: repo, store, tariff, and event bus."""

    repo: InMemoryRepo
    store: Store
    tariff: Tariff
    bus: RunBus


def _tariff() -> Tariff:
    """The RU tariff from env, falling back to the default rates."""
    return Tariff(
        c_cpu=float(os.environ.get('RU_CPU', DEFAULT_TARIFF.c_cpu)),
        c_ram=float(os.environ.get('RU_RAM', DEFAULT_TARIFF.c_ram)),
        c_disk=float(os.environ.get('RU_DISK', DEFAULT_TARIFF.c_disk)),
        c_net=float(os.environ.get('RU_NET', DEFAULT_TARIFF.c_net)),
    )


def _within(
    record: UsageRecord, since: float | None, until: float | None
) -> bool:
    """Whether a usage record falls in the [since, until] window."""
    if since is not None and record.ts < since:
        return False
    return not (until is not None and record.ts > until)


@dataclass(frozen=True)
class _Job:
    """One run to execute: the created run, its graph, and the input."""

    run: Run
    graph: Graph
    input_ref: str


def tenant_id(x_tenant_id: Annotated[str, Header(alias='X-Tenant-Id')]) -> str:
    """The calling tenant (progressive: a header now, OIDC later)."""
    return x_tenant_id


Tenant = Annotated[str, Depends(tenant_id)]


def palette() -> dict[str, list[str]]:
    """The node palette a canvas builds from: sources, steps, sinks."""
    return {
        'sources': sorted(SOURCES),
        'steps': sorted(CATALOG),
        'sinks': sorted(SINKS),
    }


def _sse(events: Iterable[Event]) -> Iterator[str]:
    """Serialize a run's events as an SSE text stream."""
    for event in events:
        payload = {
            'node': event.node,
            'phase': event.phase,
            'disposition': event.disposition,
            'ts': event.ts,
        }
        yield f'data: {json.dumps(payload)}\n\n'


def _usage_summary(records: list[UsageRecord]) -> dict[str, float]:
    """Aggregate usage into RU inputs (Compute only, from ms, for now)."""
    total_ms = sum(record.ms for record in records)
    return {
        'total_ms': total_ms,
        'compute_ru': total_ms / 1000.0 / 3600.0 * C_CPU,
        'nodes': float(len(records)),
    }


def _record_usage(
    repo: InMemoryRepo, run: Run, usage: tuple[Usage, ...]
) -> None:
    """Store one UsageRecord per node the run touched."""
    for item in usage:
        repo.add_usage(
            UsageRecord(
                id=uuid.uuid4().hex,
                tenant_id=run.tenant_id,
                run_id=run.id,
                node=item.node,
                step=item.step,
                disposition=item.disposition,
                ms=item.ms,
                ts=time.time(),
            )
        )


def _publisher(bus: RunBus, run_id: str) -> Emit:
    """An emitter that publishes a run's events onto the live bus."""

    def emit(event: Event) -> None:
        bus.publish(run_id, event)

    return emit


def _run_bg(deps: Deps, job: _Job) -> None:
    """Execute a run in the background, publishing events live."""
    run_id = job.run.id
    try:
        request = RunRequest(job.graph.spec, job.input_ref)
        emit = _publisher(deps.bus, run_id)
        result = run_graph(request, LocalCaller(deps.store), emit)
        total_ms = sum(item.ms for item in result.usage)
        done = job.run.model_copy(
            update={
                'status': 'done',
                'final_ref': result.final_ref,
                'total_ms': total_ms,
            }
        )
        deps.repo.add_run(done)
        _record_usage(deps.repo, done, result.usage)
    finally:
        deps.bus.close(run_id)


def create_api(repo: InMemoryRepo, store: Store) -> FastAPI:
    """Build the multi-tenant platform API over a repo and a store."""
    app = FastAPI(title='minion-platform')
    deps = Deps(repo=repo, store=store, tariff=_tariff(), bus=RunBus())

    @app.get('/catalog')
    def catalog() -> dict[str, list[str]]:
        return palette()

    @app.post('/graphs')
    def create_graph(body: GraphCreate, tenant: Tenant) -> Graph:
        graph = Graph(
            id=uuid.uuid4().hex,
            tenant_id=tenant,
            name=body.name,
            spec=body.spec,
            created_at=time.time(),
        )
        repo.add_graph(graph)
        return graph

    @app.get('/graphs')
    def list_graphs(tenant: Tenant) -> list[Graph]:
        return repo.list_graphs(tenant)

    @app.get('/graphs/{graph_id}')
    def get_graph(graph_id: str, tenant: Tenant) -> Graph:
        graph = repo.get_graph(tenant, graph_id)
        if graph is None:
            raise HTTPException(status_code=404, detail='graph not found')
        return graph

    @app.post('/runs')
    def start_run(body: RunStart, tenant: Tenant) -> Run:
        graph = repo.get_graph(tenant, body.graph_id)
        if graph is None:
            raise HTTPException(status_code=404, detail='graph not found')
        run = Run(
            id=uuid.uuid4().hex,
            tenant_id=tenant,
            graph_id=graph.id,
            status='running',
            final_ref=None,
            created_at=time.time(),
        )
        repo.add_run(run)
        deps.bus.open(run.id)
        job = _Job(run=run, graph=graph, input_ref=body.input_ref)
        threading.Thread(target=_run_bg, args=(deps, job), daemon=True).start()
        return run

    @app.get('/runs/{run_id}')
    def get_run(run_id: str, tenant: Tenant) -> Run:
        run = repo.get_run(tenant, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail='run not found')
        return run

    @app.get('/runs/{run_id}/events')
    def run_events(run_id: str, tenant: Tenant) -> StreamingResponse:
        if repo.get_run(tenant, run_id) is None:
            raise HTTPException(status_code=404, detail='run not found')
        return StreamingResponse(
            _sse(deps.bus.stream(run_id)), media_type='text/event-stream'
        )

    @app.get('/usage')
    def usage(tenant: Tenant) -> dict[str, float]:
        return _usage_summary(repo.list_usage(tenant))

    @app.get('/billing')
    def billing(
        tenant: Tenant,
        since: float | None = None,
        until: float | None = None,
    ) -> dict[str, float]:
        records = [
            r for r in repo.list_usage(tenant) if _within(r, since, until)
        ]
        units = resource_units(records, deps.tariff)
        return {
            'compute': units.compute,
            'memory': units.memory,
            'storage': units.storage,
            'network': units.network,
            'total': units.total,
            'nodes': float(len(records)),
        }

    web = Path(__file__).parent / 'web'
    app.mount('/ui', StaticFiles(directory=web, html=True), name='ui')
    return app


def _from_env() -> FastAPI:
    """Build the app from env (STORE_ROOT); one process, one tenant space."""
    store = LocalStore(Path(os.environ.get('STORE_ROOT', '/data/store')))
    return create_api(InMemoryRepo(), store)


app = _from_env()
"""The module-level app uvicorn serves (SKIN=api in services/serve.py)."""
