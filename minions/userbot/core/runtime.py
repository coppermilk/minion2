# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Process runtime helpers: state dir, logging, watchdog, small utilities.

Extracted from ``main``: where the on-disk log and the watchdog heartbeat
live, the log-handler set, the hang-detecting watchdog thread, and two tiny
helpers (a countdown formatter and a task canceller). Depends on stdlib only.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING

from minions.userbot.core import config

if TYPE_CHECKING:
    import asyncio
    from pathlib import Path

log = logging.getLogger('userbot')

_SECS_PER_MIN = 60
_MINS_PER_HOUR = 60
_HOURS_PER_DAY = 24
# How often the watchdog thread checks the heartbeat's age.
_WATCHDOG_POLL_SEC = 30.0


def _state_base() -> Path | None:
    """Return the base state dir, created; None when there is none to use.

    Process-level (not per-profile): the log and the watchdog heartbeat live
    here. WHERE it is, is ``config.state_base`` -- one rule, one place. What
    is local to this module is the tolerance: a log we cannot write is worth
    degrading over, so a directory that will not create reads as "no file
    here" and the console handler carries on alone.
    """
    base = config.state_base()
    if base is None:
        return None
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return base


def _log_file() -> Path | None:
    """Return the on-disk log path under the state dir, or None if gone."""
    base = _state_base()
    return base / 'aggregator.log' if base is not None else None


def _health_file() -> Path | None:
    """Return the watchdog heartbeat file (mtime = last alive time)."""
    base = _state_base()
    return base / 'health' if base is not None else None


def touch_health() -> None:
    """Stamp the heartbeat file with 'now' -- called only when proven alive."""
    path = _health_file()
    if path is None:
        return
    try:
        path.write_text(str(time.time()), encoding='ascii')
    except OSError:
        log.warning('watchdog: could not write the heartbeat file')


def watchdog(timeout: float) -> None:
    """Daemon thread: exit the process if the heartbeat goes stale (a hang).

    ``status_loop`` refreshes the heartbeat only after a successful Telegram
    probe, so a stale file means the event loop stalled OR Telethon wedged --
    cases no Docker ``restart:`` policy can catch, because the process never
    exits on its own. Being a plain OS thread it keeps running even when the
    asyncio loop is frozen, so it can ``os._exit(1)`` and let ``restart:
    always`` recreate the container. State is committed per operation, so an
    abrupt exit loses nothing. ``timeout <= 0`` disables it.
    """
    path = _health_file()
    if path is None or timeout <= 0:
        return
    while True:
        time.sleep(_WATCHDOG_POLL_SEC)
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            continue  # not written yet -> do not kill on a cold start
        if age > timeout:
            log.error(
                'watchdog: no heartbeat for %.0fs (> %.0fs); exiting to force '
                'a restart',
                age,
                timeout,
            )
            os._exit(1)  # deliberate hard exit so restart: always recreates us


def _log_handlers() -> list[logging.Handler]:
    """Console (for `docker logs`) plus a rotating file (always on disk)."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    path = _log_file()
    if path is not None:
        handlers.append(
            RotatingFileHandler(
                path, maxBytes=5_000_000, backupCount=2, encoding='utf-8'
            )
        )
    return handlers


def configure_logging() -> None:
    """Install console + rotating-file log handlers for the aggregator.

    Called once from ``main()`` -- NOT at import time -- so importing the
    package (e.g. in tests) never reconfigures the root logger. ``force=True``
    reconfigures even if an imported library already installed a root handler
    (which would make a plain basicConfig a no-op and silently drop our INFO
    level). The file handler keeps the log on disk regardless of the container
    log tab.
    """
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)-8s %(name)s: %(message)s',
        handlers=_log_handlers(),
        force=True,
    )


def fmt_eta(seconds: float) -> str:
    """Return a short duration: '45s', '8m 12s', '2h 15m' or '7d 4h'.

    The two coarsest units, always, so the number stays readable at any
    size -- and it has to work at any size, because the same span is a
    countdown in one line ("next view in 3m 10s") and an age in another
    ("last touched 7d 4h ago"). Stopping at hours made the second read
    "7211h 8m", which is a number nobody converts in their head.
    """
    total = max(0, int(seconds))
    if total < _SECS_PER_MIN:
        return f'{total}s'
    mins = total // _SECS_PER_MIN
    if mins < _MINS_PER_HOUR:
        return f'{mins}m {total % _SECS_PER_MIN}s'
    hours = mins // _MINS_PER_HOUR
    if hours < _HOURS_PER_DAY:
        return f'{hours}h {mins % _MINS_PER_HOUR}m'
    return f'{hours // _HOURS_PER_DAY}d {hours % _HOURS_PER_DAY}h'


def cancel(task: asyncio.Task[object] | None) -> None:
    """Cancel a background task if it exists (a no-op when None)."""
    if task is not None:
        task.cancel()
