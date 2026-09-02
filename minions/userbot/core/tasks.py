# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Fire-and-forget background tasks, held so they cannot vanish mid-flight.

Three engines schedule work on a timer -- a reaction, a story view, an
identity lookup -- and each kept its own set of live tasks with its own copy
of the same two moves. The set is not bookkeeping: asyncio holds only a WEAK
reference to a running task, so a task nobody keeps can be collected before
it finishes. That reason is worth stating once instead of three times.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Coroutine


def spawn(
    bucket: set[asyncio.Task[None]], work: Coroutine[object, object, None]
) -> None:
    """Start ``work`` in the background, keeping it alive until it ends."""
    task = asyncio.create_task(work)
    bucket.add(task)
    task.add_done_callback(bucket.discard)


def cancel_all(bucket: set[asyncio.Task[None]]) -> None:
    """Cancel every task still running in ``bucket``, and empty it."""
    for task in list(bucket):
        task.cancel()
    bucket.clear()
