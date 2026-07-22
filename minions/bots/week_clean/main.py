"""week-clean bot: shelve the classified week -- mechanically.

Every decision was already made during the week (sort classified in
place: prim name, EXIF fandom -- CLIP filled in whatever Gemini
could not -- and the weekly tag). The Monday run (cadence belongs
to cron -- BLUEPRINT 11: the wall clock is read only by cron; this
bot does not check the weekday) only executes what is written on
the files: strip the weekly tag off each classified image, then move
it into ``pictures/<Fandom>/`` per its EXIF fandom.

The shelving itself lives in ``minion_core.adapters.library`` so the
moderator's "clean now" command runs the same routine (REQ-ARC-001).
Nothing unclassified is touched and nothing is ever deleted here
(operator decision): leftovers wait for the next attempt.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from minion_core.adapters.admin import admin_config
from minion_core.adapters.library import clean_week
from minion_core.kernel import bot_logger
from minion_core.settings import load

if TYPE_CHECKING:
    from collections.abc import Mapping

BOT = 'week-clean'


def main(env: Mapping[str, str] | None = None) -> int:
    """One scan-act-exit run; overlap-safe, admin-disablable (REQ-RES-003)."""
    mapping = os.environ if env is None else env
    cfg = load(mapping)
    log = bot_logger(BOT, cfg.logs)
    if admin_config(cfg.state).get('week_clean_enabled') == '0':
        log.info('skipped reason=disabled_by_admin')
        return 0
    clean_week(cfg, log)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
