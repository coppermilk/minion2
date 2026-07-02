"""sort bot: one-shot batch run under the per-bot lock.

Config axis (BLUEPRINT 9): ``SOURCE_DIRS`` -- ``_inbox/`` by
default, a Downloads folder on the Windows deployment.
"""

from __future__ import annotations

import functools
import os
from typing import TYPE_CHECKING

from minion_core.adapters import llm
from minion_core.adapters import vision
from minion_core.adapters.files import BatchLock
from minion_core.kernel import bot_logger
from minion_core.settings import load
from minions.sort.passes import SortDeps
from minions.sort.passes import run_passes

if TYPE_CHECKING:
    from collections.abc import Mapping

BOT = 'sort'


def real_deps(env: Mapping[str, str]) -> SortDeps:
    """Wire the live adapters (tests inject doubles instead)."""
    spec = llm.spec_from(env)
    return SortDeps(
        namer=functools.partial(llm.name_image, spec=spec),
        embed=vision.embed_image,
    )


def main(env: Mapping[str, str] | None = None) -> int:
    """One scan-act-exit run; overlap-safe (REQ-RES-003)."""
    mapping = os.environ if env is None else env
    cfg = load(mapping)
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
