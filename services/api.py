"""Multi-tenant platform API (PLATFORM.md, section 7).

FastAPI over the Phase 3 orchestrator: a tenant stores graphs, starts runs,
and reads back the flow (SSE) and usage (RU). The tenant comes from a header
now (``X-Tenant-Id``); real auth (OIDC) is a progressive follow-up. Every
access is tenant-scoped. ``/catalog`` is the palette a React Flow canvas
builds nodes from (Phase 5).

Runs execute synchronously and their events are captured for SSE replay;
background execution with a live event bus is the next refinement.
"""

from __future__ import annotations

import json
import os
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

from minion_core.events import Collector
from minion_core.graph import SINKS
from minion_core.graph import SOURCES
from minions.service import CATALOG
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
    from collections.abc import Iterator

    from minion_core.events import Event
    from services.orchestrate import Usage
    from services.store import Store

C_CPU = 0.05
"""Internal Compute tariff (RU per vCPU-hour); decoupled from public price."""


@dataclass(frozen=True)
class Deps:
    """The server's dependencies: the repository and the object store."""

    repo: InMemoryRepo
    store: Store


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


def _sse(events: list[Event]) -> Iterator[str]:
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
            )
        )


def _execute(deps: Deps, job: _Job) -> Run:
    """Run the graph, capture events and usage; return the done run."""
    collector = Collector()
    request = RunRequest(job.graph.spec, job.input_ref)
    result = run_graph(request, LocalCaller(deps.store), collector)
    done = job.run.model_copy(
        update={'status': 'done', 'final_ref': result.final_ref}
    )
    deps.repo.add_run(done)
    deps.repo.set_events(done.id, collector.events)
    _record_usage(deps.repo, done, result.usage)
    return done


def create_api(repo: InMemoryRepo, store: Store) -> FastAPI:
    """Build the multi-tenant platform API over a repo and a store."""
    app = FastAPI(title='minion-platform')
    deps = Deps(repo=repo, store=store)

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
        return _execute(
            deps, _Job(run=run, graph=graph, input_ref=body.input_ref)
        )

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
            _sse(repo.list_events(run_id)), media_type='text/event-stream'
        )

    @app.get('/usage')
    def usage(tenant: Tenant) -> dict[str, float]:
        return _usage_summary(repo.list_usage(tenant))

    web = Path(__file__).parent / 'web'
    app.mount('/ui', StaticFiles(directory=web, html=True), name='ui')
    return app


def _from_env() -> FastAPI:
    """Build the app from env (STORE_ROOT); one process, one tenant space."""
    store = LocalStore(Path(os.environ.get('STORE_ROOT', '/data/store')))
    return create_api(InMemoryRepo(), store)


app = _from_env()
"""The module-level app uvicorn serves (SKIN=api in services/serve.py)."""
