"""Mode B: walk a graph.json, call each Step's service, thread the refs.

The orchestrator is the distributed run mode (PLATFORM.md, section 3): it
takes a graph spec and an input ref, runs the ordered Step nodes as service
calls, feeding each output ref into the next, and emits the Phase 1.5
events (so Mode A and Mode B animate the same way) while collecting a Usage
record per node (ms from the service -- RU computed later, not billed here).

A Caller abstracts the transport: LocalCaller runs the core in-process
(offline, tests); HttpCaller POSTs to a service's /run (the real
distributed path). Both speak the same ServiceResult, so the walk is one
piece of code.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Protocol

from minion_core.events import Event
from minion_core.graph import assign_ids
from services.core import ServiceRequest
from services.core import ServiceResult
from services.core import run_service

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx

    from minion_core.events import Emit
    from minion_core.graph import Node
    from services.store import Store


@dataclass(frozen=True)
class Usage:
    """One node's resource record (RU inputs; billing is later)."""

    node: str
    step: str
    disposition: str
    ms: float


@dataclass(frozen=True)
class RunRequest:
    """One distributed run: a graph spec over an input ref."""

    spec: Node
    input_ref: str


@dataclass(frozen=True)
class RunResult:
    """The run outcome: where the item ended, and per-node usage."""

    final_ref: str | None
    usage: tuple[Usage, ...]


class Caller(Protocol):
    """Run one Step over an input ref; the transport is the impl."""

    def call(self, step: str, input_ref: str) -> ServiceResult:
        """Return the Step's ServiceResult for this input."""
        ...


class LocalCaller:
    """In-process transport: the service core, no network (offline/tests)."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def call(self, step: str, input_ref: str) -> ServiceResult:
        """Run the Step in-process over the stored input."""
        return run_service(ServiceRequest(step, input_ref), self._store)


class HttpCaller:
    """Distributed transport: POST to each Step service's /run."""

    def __init__(
        self, resolve: Callable[[str], str], client: httpx.Client
    ) -> None:
        self._resolve = resolve
        self._client = client

    def call(self, step: str, input_ref: str) -> ServiceResult:
        """POST the input ref to the Step's service; map the reply back."""
        url = f'{self._resolve(step)}/run'
        data = self._client.post(url, json={'input_ref': input_ref}).json()
        return ServiceResult(
            output_ref=data['output_ref'],
            disposition=data['disposition'],
            reason=data['reason'],
            ms=data['ms'],
        )


def steps_of(spec: Node) -> list[tuple[str, str]]:
    """The ordered (node id, step name) pairs a graph runs as services."""
    assign_ids(spec)
    return [
        (node['id'], node['step'])
        for stage in spec['stages']
        for node in stage.get('merge', [stage])
        if 'step' in node
    ]


def _fire(emit: Emit | None, event: Event) -> None:
    """Emit an event when an emitter is present."""
    if emit is not None:
        emit(event)


def _left(node: str, result: ServiceResult) -> Event:
    """The 'left' event carrying the service's verdict."""
    return Event(node, 'left', result.disposition, result.reason, time.time())


def run_graph(
    req: RunRequest, caller: Caller, emit: Emit | None = None
) -> RunResult:
    """Run the spec's Steps as service calls, threading output -> input."""
    ref = req.input_ref
    usage = []
    for node, step in steps_of(req.spec):
        _fire(emit, Event(node, 'entered', '', '', time.time()))
        result = caller.call(step, ref)
        _fire(emit, _left(node, result))
        usage.append(Usage(node, step, result.disposition, result.ms))
        if result.output_ref is None:
            break
        ref = result.output_ref
    return RunResult(ref, tuple(usage))
