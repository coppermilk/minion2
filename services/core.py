"""Protocol-neutral service core: run one Step over a stored input.

Both the HTTP and MCP skins call ``run_service`` with a ``make`` factory that
builds the one Step (or chain) this service serves -- so the core imports no
catalog and a service knows only its own Step. Stateless: a fresh temp DRIVE
per request, the input fetched in by reference, the Step run via ``invoke``,
the result put back to the store. ``ms`` is the timing seam.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from typing import TypeAlias

from minion_core.kernel import Disposition
from minion_core.service import Call
from minion_core.service import invoke
from minion_core.settings import load
from services.store import child_refs

if TYPE_CHECKING:
    from collections.abc import Callable

    from minion_core.kernel import Stage
    from minion_core.kernel import Verdict
    from minion_core.settings import Settings
    from services.store import Store

Make: TypeAlias = 'Callable[[Settings], Stage]'
"""Builds the one Step (or chain) a service serves -- no catalog needed."""


@dataclass(frozen=True)
class ServiceRequest:
    """One service call: which Step, over which stored input."""

    step: str
    input_ref: str


@dataclass(frozen=True)
class ServiceResult:
    """The outcome: where the output landed, the verdict, the timing.

    ``outputs`` is the full set of stored refs (one for a file result, N
    for a directory result like frames). ``output_ref`` is the single
    object when there is exactly one, else None.
    """

    output_ref: str | None
    disposition: str
    reason: str
    ms: float
    outputs: list[str] = field(default_factory=list)


def run_service(
    req: ServiceRequest, store: Store, make: Make
) -> ServiceResult:
    """Fetch the input, run the Step built by ``make``, put the output."""
    with TemporaryDirectory() as tmp:
        work = Path(tmp)
        cfg = load({'DRIVE': str(work)})
        into = cfg.bot_dir(req.step)
        src = store.fetch(req.input_ref, into)
        start = time.monotonic()
        verdict = invoke(make(cfg), Call(src=src, dest=into))
        ms = (time.monotonic() - start) * 1000.0
        primary, outputs = _store_output(store, req, verdict)
        return ServiceResult(
            output_ref=primary,
            disposition=verdict.disposition.value,
            reason=verdict.reason,
            ms=ms,
            outputs=outputs,
        )


def _store_output(
    store: Store, req: ServiceRequest, verdict: Verdict
) -> tuple[str | None, list[str]]:
    """Put a delivered result; return the single ref (if one) and all refs."""
    if verdict.disposition is not Disposition.DELIVERED:
        return None, []
    result = verdict.result
    if result is None:
        return None, []
    return _store_result(store, f'{req.step}/{result.name}', result)


def _store_result(
    store: Store, key: str, result: Path
) -> tuple[str | None, list[str]]:
    """A file result is one ref; a directory result is one ref per file."""
    if result.is_file():
        ref = store.put(key, result)
        return ref, [ref]
    refs = list(child_refs(store, key, result))
    primary = refs[0] if len(refs) == 1 else None
    return primary, refs
