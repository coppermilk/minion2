"""Protocol-neutral service core: run one Step over a stored input.

Both the HTTP and MCP skins call ``run_service``; it is the one place the
data plane meets the Phase 0 dispatcher. Stateless: a fresh temp DRIVE per
request, the input fetched in by reference, the Step run via ``invoke``,
the result put back to the store. The Step (the IP) is never touched, and
``ms`` is the metering point (PLATFORM.md, sections 3 and 6).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from minion_core.kernel import Disposition
from minion_core.service import Call
from minion_core.service import invoke
from minion_core.settings import load
from minions.service import build

if TYPE_CHECKING:
    from minion_core.kernel import Verdict
    from services.store import Store


@dataclass(frozen=True)
class ServiceRequest:
    """One service call: which Step, over which stored input."""

    step: str
    input_ref: str


@dataclass(frozen=True)
class ServiceResult:
    """The outcome: where the output landed, the verdict, the timing."""

    output_ref: str | None
    disposition: str
    reason: str
    ms: float


def run_service(req: ServiceRequest, store: Store) -> ServiceResult:
    """Fetch the input, run the Step, put the output; time the Step."""
    with TemporaryDirectory() as tmp:
        return _run(req, store, Path(tmp))


def _run(req: ServiceRequest, store: Store, work: Path) -> ServiceResult:
    cfg = load({'DRIVE': str(work)})
    into = cfg.bot_dir(req.step)
    src = store.fetch(req.input_ref, into)
    start = time.monotonic()
    verdict = invoke(build(req.step, cfg), Call(src=src, dest=into))
    ms = (time.monotonic() - start) * 1000.0
    return ServiceResult(
        output_ref=_output_ref(store, req, verdict),
        disposition=verdict.disposition.value,
        reason=verdict.reason,
        ms=ms,
    )


def _output_ref(
    store: Store, req: ServiceRequest, verdict: Verdict
) -> str | None:
    """The ref of a delivered file result, or None (dir/non-delivery)."""
    if verdict.disposition is not Disposition.DELIVERED:
        return None
    result = verdict.result
    if result is None or not result.is_file():
        return None
    return store.put(f'{req.step}/{result.name}', result)
