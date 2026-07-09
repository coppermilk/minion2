"""Run one Step as a service: an input file -> a Verdict (Phase 0).

The dispatcher is catalog-neutral: it wraps an input file as a synthetic
Job, drives it through the given Step using the belt's own crash guard
(REQ-KRN-001), and returns the resulting Verdict. The catalog of concrete
Steps lives at the top level (``minions/service.py``), so the kernel layer
never imports a bot (import direction; test: adapters never import bots).

This is the orchestrator-neutral seam: any front-end -- the current Python
graphs, a future visual canvas, or an external trigger over local HTTP --
reaches the same Step through ``invoke`` without the IP moving or changing
(ORCHESTRATION.md, Phase 0).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from minion_core.kernel import Disposition
from minion_core.kernel import Envelope
from minion_core.kernel import Job
from minion_core.kernel import Origin
from minion_core.kernel import Verdict

if TYPE_CHECKING:
    from pathlib import Path

    from minion_core.kernel import Step

SERVICE = 'svc'
"""``Origin.source`` for a job that entered through the service, not a dock."""


@dataclass(frozen=True)
class Call:
    """One service invocation: an input file and its output directory."""

    src: Path
    dest: Path


def job_of(call: Call) -> Job:
    """Wrap an input file as a synthetic service Job."""
    origin = Origin(source=SERVICE, ref=str(call.src))
    return Job(src=call.src, dest=call.dest, stem=call.src.stem, origin=origin)


def invoke(step: Step, call: Call) -> Verdict:
    """Run one Step over one input, reusing the belt's crash guard.

    A Step yields exactly one advanced Envelope per input; its verdict is
    the service result. A guard failure (REQ-KRN-001) already maps to
    FAILED inside the belt, so no exception escapes here.
    """
    out = list(step(iter((Envelope(job_of(call)),))))
    return _verdict_of(out)


def _verdict_of(out: list[Envelope]) -> Verdict:
    """The single verdict a one-input Step run produces (defensive)."""
    if not out or out[0].verdict is None:
        return Verdict(Disposition.FAILED, reason='no_verdict')
    return out[0].verdict
