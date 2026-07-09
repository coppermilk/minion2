"""Phase 0 dispatcher: a Step invoked as a service equals the belt.

The service seam must not change what a Step decides: for one input the
dispatcher's verdict is exactly the Step's own verdict (golden), it
reuses the kernel crash guard (REQ-KRN-001), and every catalog name
resolves to a constructible Step (import direction holds).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from minion_core.adapters.fetch import FetchLink
from minion_core.kernel import Disposition
from minion_core.kernel import Step
from minion_core.kernel import Verdict
from minion_core.service import Call
from minion_core.service import invoke
from minion_core.service import job_of
from minions.service import CATALOG
from minions.service import build
from minions.service import run
from tests.conftest import make_cfg

if TYPE_CHECKING:
    from pathlib import Path

    from minion_core.kernel import Job


class _Echo(Step):
    """A test Step: deliver the input unchanged."""

    def process(self, job: Job) -> Verdict:
        return Verdict(Disposition.DELIVERED, result=job.src, reply='ok')


class _Boom(Step):
    """A test Step that raises: the guard must catch it."""

    def process(self, job: Job) -> Verdict:
        raise RuntimeError('boom')


def test_job_carries_a_service_origin(tmp_path: Path) -> None:
    """A service job is provenance 'svc', stem from the input name."""
    src = tmp_path / 'in.txt'
    job = job_of(Call(src=src, dest=tmp_path / 'out'))
    assert job.origin.source == 'svc'
    assert job.src == src
    assert job.stem == 'in'


def test_dispatcher_returns_the_step_verdict(tmp_path: Path) -> None:
    """The verdict the Step decides is the verdict the service returns."""
    src = tmp_path / 'a.txt'
    src.write_text('x', encoding='ascii')
    verdict = invoke(_Echo(), Call(src=src, dest=tmp_path))
    assert verdict.disposition is Disposition.DELIVERED
    assert verdict.result == src
    assert verdict.reply == 'ok'


def test_dispatcher_guards_a_raising_step(tmp_path: Path) -> None:
    """REQ-KRN-001 reused: a raising Step maps to FAILED, no crash."""
    src = tmp_path / 'a.txt'
    src.write_text('x', encoding='ascii')
    verdict = invoke(_Boom(), Call(src=src, dest=tmp_path))
    assert verdict.disposition is Disposition.FAILED
    assert verdict.reason == 'step_crashed'


def test_service_matches_direct_process(tmp_path: Path) -> None:
    """Golden: the service path == a direct process() call.

    FetchLink passes a non-link input through unchanged (no network,
    no disk write), so the two paths are comparable on one input.
    """
    cfg = make_cfg(tmp_path / 'drive')
    src = tmp_path / 'clip.mp4'
    src.write_bytes(b'video')
    call = Call(src=src, dest=tmp_path)
    direct = FetchLink(cfg).process(job_of(call))
    served = run('fetch', call, cfg)
    assert served == direct
    assert served.disposition is Disposition.DELIVERED
    assert served.result == src


def test_catalog_builds_every_registered_step(tmp_path: Path) -> None:
    """Import direction + names resolve: each name builds a Step."""
    cfg = make_cfg(tmp_path / 'drive')
    for name in CATALOG:
        assert isinstance(build(name, cfg), Step)
