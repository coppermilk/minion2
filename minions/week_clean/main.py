"""week-clean bot: strip the weekly EXIF tag, clear ``_inbox/``.

Cadence belongs to cron (BLUEPRINT 11: the wall clock is read only
by cron) -- this bot does not check the weekday itself.
"""

from __future__ import annotations

import itertools
import os
from typing import TYPE_CHECKING

from minion_core.adapters.files import BatchLock
from minion_core.adapters.files import has_week
from minion_core.adapters.files import strip_week
from minion_core.adapters.vision import IMAGE_EXTS
from minion_core.kernel import bot_logger
from minion_core.settings import load

if TYPE_CHECKING:
    import logging
    from collections.abc import Mapping

    from minion_core.settings import Settings

BOT = 'week-clean'


def _strip_tags(cfg: Settings, log: logging.Logger) -> None:
    """Remove the weekly tag across the library, scan capped."""
    tagged = (p for p in sorted(cfg.pictures.rglob('*'))
              if p.suffix.lower() in IMAGE_EXTS)
    for path in itertools.islice(tagged, cfg.max_embedding_scan):
        if has_week(path, cfg.week_tag):
            strip_week(path, cfg.week_tag)
            log.info('untagged src=%s', path.name)


def _clear_inbox(cfg: Settings, log: logging.Logger) -> None:
    """Empty the ingest drop folder."""
    if not cfg.inbox.is_dir():
        return
    for path in sorted(cfg.inbox.iterdir()):
        if path.is_file():
            path.unlink()
            log.info('cleared src=%s', path.name)


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
        _strip_tags(cfg, log)
        _clear_inbox(cfg, log)
    finally:
        lock.release()
    log.info('cleaned')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
