# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Build the Telethon client with gentle networking and a crash-safe session.

Both entry points (``main`` and ``login``) create the aggregator's
``TelegramClient`` here, so the two account-friendliness measures live in ONE
place:

* **Gentle requests.** ``flood_sleep_threshold`` is raised well above
  Telethon's 60s default, so when Telegram answers a request with a FloodWait
  the client PATIENTLY sleeps it off and retries instead of raising -- the
  single most important "don't get the account limited" behaviour for a
  userbot under sustained automated load (reactions, story views, DMs). A
  flood is per-request and each engine is its own task, so one sleeping call
  never blocks the others or the liveness probe.

* **A session file that survives a hard kill.** Telethon's SQLite session runs
  in the default ``DELETE`` journal, which is NOT crash-safe: the in-process
  watchdog turns a hang into a hard ``os._exit(1)``, and a kill landing mid
  session-commit left the ``.session`` file corrupt ("database disk image is
  malformed") -- forcing a manual re-login every couple of weeks. WAL mode
  fixes that at the source: SQLite recovers the write-ahead log on the next
  open, so an abrupt exit can no longer corrupt the file.
  ``synchronous=NORMAL`` (safe under WAL) also trims fsync churn on the NAS.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telethon import TelegramClient

from minions.userbot.core.config import _load_runtime

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger('userbot')

# Sleep off Telegram flood waits up to this many seconds (rather than raising).
# An hour absorbs every transient rate limit an idle-to-busy userbot hits; a
# genuinely huge flood (very rare) still raises and is handled per call-site.
# Override with runtime.flood_sleep_threshold_sec in the constants JSON.
DEFAULT_FLOOD_SLEEP_THRESHOLD = 3600.0


def build_client(
    session_path: Path, api_id: int, api_hash: str
) -> TelegramClient:
    """Create the aggregator's client: gentle floods + a crash-safe session."""
    rt = _load_runtime()
    threshold = float(
        rt.get('flood_sleep_threshold_sec', DEFAULT_FLOOD_SLEEP_THRESHOLD)
    )
    client = TelegramClient(
        str(session_path),
        api_id,
        api_hash,
        flood_sleep_threshold=threshold,
    )
    _harden_session(client)
    return client


def _harden_session(client: TelegramClient) -> None:
    """Put the file session in WAL mode so a hard os._exit() cannot corrupt it.

    Best-effort and idempotent (WAL is stored in the DB header, so it persists
    once set): an in-memory session, a missing connection, or a Telethon
    internal change just leaves the default journal in place with a warning.
    """
    session = getattr(client, 'session', None)
    conn = getattr(session, '_conn', None)
    if conn is None:
        return  # in-memory or not yet opened -- nothing to harden
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.commit()
    except Exception:  # noqa: BLE001 -- hardening is optional, never fatal
        log.warning('session: could not enable WAL; keeping default journal')
        return
    log.info('session: WAL journal enabled (crash-safe on hard exit)')
