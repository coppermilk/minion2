"""week-clean bot: shelve the classified week -- mechanically.

Every decision was already made during the week (sort classified in
place: prim name, EXIF fandom -- CLIP filled in whatever Gemini could
not -- and the weekly tag). The run only executes what is written on the
files: strip the weekly tag off each classified image, then move it into
``pictures/<Fandom>/`` per its EXIF fandom.

Cadence is a moderator cron (``week_clean_cron``, default Monday 09:00):
cron polls this bot every minute and it fires when its schedule is due.
The shelving itself lives in ``minion_core.adapters.library`` so the
moderator's "clean now" command runs the same routine (REQ-ARC-001).
Nothing unclassified is touched and nothing is ever deleted here.
"""

from __future__ import annotations

import os
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

from minion_core.adapters.admin import admin_config
from minion_core.adapters.schedule import cron_due
from minion_core.kernel import bot_logger
from minion_core.settings import load

if TYPE_CHECKING:
    from collections.abc import Mapping

BOT = 'week-clean'


def main(env: Mapping[str, str] | None = None) -> int:
    """One scan-act-exit run when the cron is due; else a clean no-op."""
    mapping = os.environ if env is None else env
    cfg = load(mapping)
    log = bot_logger(BOT, cfg.logs)
    admin = admin_config(cfg.state)
    if admin.get('week_clean_enabled') == '0':
        log.info('skipped reason=disabled_by_admin')
        return 0
    if not cron_due(admin.get('week_clean_cron'), datetime.now(tz=UTC)):
        log.info('skipped reason=not_scheduled')
        return 0
    # Lazy: importing clean_week pulls the vision stack (torch); do it
    # only on a run that actually fires, not on every polled minute.
    from minion_core.adapters.library import clean_week  # noqa: PLC0415

    clean_week(cfg, log)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
