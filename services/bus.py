"""A live per-run event bus (Phase 6 follow-up): real-time SSE.

A background run publishes events here as they happen; an SSE subscriber
streams them live, and a late subscriber still gets the full history, since
each run buffers its events until it closes. In-memory and thread-safe; a
durable bus (Redis Streams / NATS) is the cloud swap (PLATFORM.md 5).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from minion_core.events import Event

_WAIT_SEC = 1.0


@dataclass
class _Channel:
    """One run's event buffer, completion flag, and condition."""

    events: list[Event] = field(default_factory=list)
    done: bool = False
    cond: threading.Condition = field(default_factory=threading.Condition)


def _drain(channel: _Channel, seen: int) -> tuple[list[Event], bool]:
    """Wait for new events or completion; return the new batch + done."""
    with channel.cond:
        while seen >= len(channel.events) and not channel.done:
            channel.cond.wait(timeout=_WAIT_SEC)
        return channel.events[seen:], channel.done


class RunBus:
    """Per-run publish/subscribe over buffered, live event streams."""

    def __init__(self) -> None:
        self._channels: dict[str, _Channel] = {}
        self._lock = threading.Lock()

    def open(self, run_id: str) -> None:
        """Register a run before it starts emitting."""
        with self._lock:
            self._channels[run_id] = _Channel()

    def publish(self, run_id: str, event: Event) -> None:
        """Append one event and wake subscribers."""
        channel = self._get(run_id)
        with channel.cond:
            channel.events.append(event)
            channel.cond.notify_all()

    def close(self, run_id: str) -> None:
        """Mark a run complete and wake subscribers."""
        channel = self._get(run_id)
        with channel.cond:
            channel.done = True
            channel.cond.notify_all()

    def stream(self, run_id: str) -> Iterator[Event]:
        """Yield a run's events live, from the start, until it closes."""
        channel = self._get(run_id)
        seen = 0
        while True:
            batch, done = _drain(channel, seen)
            seen += len(batch)
            yield from batch
            if done:
                return

    def _get(self, run_id: str) -> _Channel:
        with self._lock:
            return self._channels[run_id]
