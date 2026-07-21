"""Library shelving: move the classified week into ``pictures/<Fandom>/``.

The mechanical end-of-week operation, factored out of the week-clean bot
so the moderator's "clean now" command runs the exact same routine
(REQ-ARC-001: neither bot imports the other -- both call this). Every
decision was already made during the week (sort classified in place: prim
name, EXIF fandom, weekly tag); this only executes what the files carry.

Nothing unclassified is touched and nothing is ever deleted (operator
decision): leftovers wait for the next attempt. The ``BatchLock`` makes the
manual command and the Monday cron overlap-safe (REQ-RES-003).
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING

from minion_core.adapters.files import PRIM_NAMED
from minion_core.adapters.files import BatchLock
from minion_core.adapters.files import move_atomic
from minion_core.adapters.files import next_free_path
from minion_core.adapters.files import next_free_prim
from minion_core.adapters.files import read_fandom
from minion_core.adapters.files import strip_week
from minion_core.adapters.vision import IMAGE_EXTS
from minion_core.settings import UNKNOWN

if TYPE_CHECKING:
    import logging
    from pathlib import Path

    from minion_core.settings import Settings

LOCK_NAME = 'week-clean.lock'
"""One lock for both the cron run and the manual command (shared state)."""


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


def shelve_week(cfg: Settings, log: logging.Logger) -> None:
    """Untag and move the classified week into ``pictures/``.

    The fandom rides in EXIF (files.tag_fandom); an image that lost it
    (non-JPEG, stripped metadata) lands in Unknown, where sort's Re-place
    pass rescues it. Unclassified files are not touched.
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


def shelve_scripts(cfg: Settings, log: logging.Logger) -> None:
    """Archive the week's ``.gdoc`` script pointers into ``Scripts/``.

    During the week a ``.gdoc`` is a read-only hint (adapters.scripts); the
    Monday run moves the pointer out of the inbox into ``Scripts/`` -- never
    deleted -- so the inbox starts the next week clean and the doc is kept.
    """
    if not cfg.inbox.is_dir():
        return
    cfg.scripts.mkdir(parents=True, exist_ok=True)
    for gdoc in sorted(cfg.inbox.glob('*.gdoc')):
        target = move_atomic(gdoc, next_free_path(cfg.scripts / gdoc.name))
        log.info('script_shelved src=%s', target.name)


def clean_week(cfg: Settings, log: logging.Logger) -> bool:
    """One scan-act-exit clean under the batch lock; overlap-safe.

    Shelves the classified week into ``pictures/`` and archives the week's
    ``.gdoc`` scripts into ``Scripts/``. Returns whether the clean ran
    (``False`` when another run holds the lock), so a caller can tell the
    operator "already running".
    """
    lock = BatchLock(cfg.state / LOCK_NAME)
    if not lock.acquire():
        log.warning('skipped reason=batch_locked')
        return False
    try:
        shelve_week(cfg, log)
        shelve_scripts(cfg, log)
    finally:
        lock.release()
    log.info('cleaned')
    return True
