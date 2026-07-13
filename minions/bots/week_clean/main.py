"""week-clean bot: shelve the classified week -- mechanically.

Every decision was already made during the week (sort classified in
place: prim name, EXIF fandom -- CLIP filled in whatever Gemini
could not -- and the weekly tag). The Monday run (cadence belongs
to cron -- BLUEPRINT 11: the wall clock is read only by cron; this
bot does not check the weekday) only executes what is written on
the files:

1. strip the weekly tag off each classified image;
2. move it into ``pictures/<Fandom>/`` per its EXIF fandom.

Nothing unclassified is touched and nothing is ever deleted here
(operator decision): leftovers wait for the next attempt.
"""

from __future__ import annotations

import itertools
import os
from typing import TYPE_CHECKING

from minion_core.adapters.files import PRIM_NAMED
from minion_core.adapters.files import BatchLock
from minion_core.adapters.files import move_atomic
from minion_core.adapters.files import next_free_prim
from minion_core.adapters.files import read_fandom
from minion_core.adapters.files import strip_week
from minion_core.adapters.vision import IMAGE_EXTS
from minion_core.kernel import bot_logger
from minion_core.settings import UNKNOWN
from minion_core.settings import load

if TYPE_CHECKING:
    import logging
    from collections.abc import Mapping
    from pathlib import Path

    from minion_core.settings import Settings

BOT = 'week-clean'


def _classified(root: Path, cap: int) -> list[Path]:
    """Prim-named images directly under ``root``, scan capped."""
    if not root.is_dir():
        return []
    found = (
        p
        for p in sorted(root.iterdir())
        if p.suffix.lower() in IMAGE_EXTS and PRIM_NAMED.match(p.name)
    )
    return list(itertools.islice(found, cap))


def _shelve_week(cfg: Settings, log: logging.Logger) -> None:
    """Untag and move the classified week into ``pictures/``.

    The fandom rides in EXIF (files.tag_fandom); an image that lost
    it (non-JPEG, stripped metadata) lands in Unknown, where sort's
    Re-place pass rescues it. Unclassified files are not touched.
    """
    dirs = cfg.source_dirs or (cfg.inbox,)
    cap = cfg.max_embedding_scan
    for path in (p for d in dirs for p in _classified(d, cap)):
        strip_week(path, cfg.week_tag)
        fandom = read_fandom(path) or UNKNOWN
        target = move_atomic(
            path, next_free_prim(cfg.pictures / fandom / path.name)
        )
        log.info('shelved src=%s fandom=%s', target.name, fandom)


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
        _shelve_week(cfg, log)
    finally:
        lock.release()
    log.info('cleaned')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
