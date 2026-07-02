"""sort bot: the four passes, one-shot or watch-triggered.

Config axes (BLUEPRINT 9): ``SOURCE_DIRS`` -- ``_inbox/`` by
default, a Downloads folder on the Windows deployment; and
``SORT_WATCH=1`` -- a streaming trigger: a Folder dock per source
dir fires a locked pass run the moment a new stable image lands
(REQ-KRN-005 keeps half-written files out), so Downloads and
``_inbox`` sort instantly instead of waiting for a cron tick.
"""

from __future__ import annotations

import functools
import logging
import operator
import os
from typing import TYPE_CHECKING

from minion_core.adapters import llm
from minion_core.adapters import vision
from minion_core.adapters.files import BatchLock
from minion_core.kernel import Disposition
from minion_core.kernel import Folder
from minion_core.kernel import FolderSpec
from minion_core.kernel import SeenPaths
from minion_core.kernel import Step
from minion_core.kernel import Verdict
from minion_core.kernel import bot_logger
from minion_core.kernel import run
from minion_core.settings import load
from minions.sort.passes import SortDeps
from minions.sort.passes import run_passes

if TYPE_CHECKING:
    from collections.abc import Mapping

    from minion_core.kernel import Job
    from minion_core.kernel import Stage
    from minion_core.settings import Settings

BOT = 'sort'

_LOG = logging.getLogger(BOT)


def real_deps(env: Mapping[str, str]) -> SortDeps:
    """Wire the live adapters (tests inject doubles instead)."""
    spec = llm.spec_from(env)
    return SortDeps(
        namer=functools.partial(llm.name_image, spec=spec),
        embed=vision.embed_image,
    )


class SortTrigger(Step):
    """Run the four passes when a new stable image lands.

    The passes move the trigger file themselves, so there is no
    disposal sink; the per-bot lock keeps a slow run from
    overlapping a fresh trigger (REQ-RES-003).
    """

    def __init__(self, cfg: Settings, deps: SortDeps) -> None:
        self._cfg = cfg
        self._deps = deps

    def process(self, job: Job) -> Verdict:
        """One locked pass run per trigger."""
        _LOG.info('triggered src=%s', job.src)
        lock = BatchLock(self._cfg.state / f'{BOT}.lock')
        if not lock.acquire():
            return Verdict(Disposition.SKIPPED, reason='batch_locked')
        try:
            run_passes(self._cfg, self._deps)
        finally:
            lock.release()
        return Verdict(Disposition.DELIVERED, reply='sorted')


def build_watch(cfg: Settings, deps: SortDeps) -> Stage:
    """One Folder dock per source dir, all feeding one trigger."""
    seen = SeenPaths(cfg.seen_paths_max)
    dirs = cfg.source_dirs or (cfg.inbox,)
    docks: list[Stage] = [
        Folder(
            FolderSpec(
                root=d,
                dest=d,
                exts=vision.IMAGE_EXTS,
                poll_sec=cfg.poll_sec,
            ),
            seen,
        )
        for d in dirs
    ]
    head = functools.reduce(operator.or_, docks)
    return head >> SortTrigger(cfg, deps)


def main(env: Mapping[str, str] | None = None) -> int:
    """Watch daemon when ``SORT_WATCH=1``; one-shot run otherwise."""
    mapping = os.environ if env is None else env
    cfg = load(mapping)
    if cfg.sort_watch:
        vision.warm_embedder()  # resources at init, never mid-flight
        return run(BOT, build_watch(cfg, real_deps(mapping)), cfg.logs)
    log = bot_logger(BOT, cfg.logs)
    lock = BatchLock(cfg.state / f'{BOT}.lock')
    if not lock.acquire():
        log.warning('skipped reason=batch_locked')
        return 0
    try:
        run_passes(cfg, real_deps(mapping))
    finally:
        lock.release()
    log.info('sorted')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
