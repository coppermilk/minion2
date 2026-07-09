"""Phase 6 follow-up: the live per-run event bus.

Hermetic (stdlib + kernel Event, no web stack): a subscriber gets a run's
events whether it arrives before or after they were published, and the
stream ends when the run closes -- across threads.
"""

from __future__ import annotations

import threading
import time

from minion_core.events import Event
from services.bus import RunBus


def _ev(phase: str) -> Event:
    return Event('n', phase, '', '', 0.0)


def test_late_subscriber_gets_the_full_history() -> None:
    """Events published before subscribing are still streamed, then end."""
    bus = RunBus()
    bus.open('r')
    bus.publish('r', _ev('entered'))
    bus.publish('r', _ev('left'))
    bus.close('r')
    assert [e.phase for e in bus.stream('r')] == ['entered', 'left']


def test_live_stream_across_threads() -> None:
    """A subscriber waiting on an open run receives events as they land."""
    bus = RunBus()
    bus.open('r')
    seen: list[str] = []

    def consume() -> None:
        seen.extend(e.phase for e in bus.stream('r'))

    reader = threading.Thread(target=consume)
    reader.start()
    time.sleep(0.05)  # subscribe first, then produce
    bus.publish('r', _ev('entered'))
    bus.publish('r', _ev('left'))
    bus.close('r')
    reader.join(timeout=5)
    assert seen == ['entered', 'left']
