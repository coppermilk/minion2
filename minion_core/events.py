"""Event taps: watch items flow through graph nodes (Phase 1.5).

A Tap wraps one node Stage and emits an Event as each item enters and
leaves it, so the same in-process belt that runs offline can also feed a
live view (files moving along the graph) and, later, per-node metering --
without touching the Steps (PLATFORM.md, Phase 1.5). Taps are added by the
loader only when an emitter is supplied, so the default offline path is
unchanged and zero-cost.

The emitter is called from the belt's threads (a merged dock runs each
source on its own thread), so an Emit must be thread-safe; Collector is a
ready thread-safe sink for tests and embedded use. An Event carries wall
time (``ts``); a consumer diffs a node's entered/left to get its duration,
so no timing state lives on the tap.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import TypeAlias

from minion_core.kernel import Stage

if TYPE_CHECKING:
    from collections.abc import Callable

    from minion_core.kernel import Stream
    from minion_core.kernel import Verdict


@dataclass(frozen=True)
class Event:
    """One item crossing one node boundary."""

    node: str
    phase: str
    disposition: str
    reason: str
    ts: float


Emit: TypeAlias = 'Callable[[Event], None]'
"""A thread-safe event sink."""


def _disposition(verdict: Verdict | None) -> str:
    """The verdict's disposition value, or empty before one exists."""
    return verdict.disposition.value if verdict is not None else ''


def _reason(verdict: Verdict | None) -> str:
    """The verdict's stable reason code, if any."""
    return verdict.reason if verdict is not None else ''


class Tap(Stage):
    """Wrap a node, emitting an Event as each item enters and leaves."""

    def __init__(self, inner: Stage, node: str, emit: Emit) -> None:
        self._inner = inner
        self._node = node
        self._emit = emit

    def __call__(self, up: Stream) -> Stream:
        """Emit on entry, run the node, emit on exit."""
        return self._leaving(self._inner(self._entering(up)))

    def _entering(self, up: Stream) -> Stream:
        for env in up:
            self._emit(Event(self._node, 'entered', '', '', time.time()))
            yield env

    def _leaving(self, up: Stream) -> Stream:
        for env in up:
            verdict = env.verdict
            self._emit(
                Event(
                    self._node,
                    'left',
                    _disposition(verdict),
                    _reason(verdict),
                    time.time(),
                )
            )
            yield env


def tap(inner: Stage, node: str, emit: Emit) -> Stage:
    """Wrap a node Stage with an event tap."""
    return Tap(inner, node, emit)


class Collector:
    """A thread-safe Emit that records events (tests, embedded use)."""

    def __init__(self) -> None:
        self._events: list[Event] = []
        self._lock = threading.Lock()

    def __call__(self, event: Event) -> None:
        """Record one event."""
        with self._lock:
            self._events.append(event)

    @property
    def events(self) -> list[Event]:
        """A snapshot copy of the recorded events."""
        with self._lock:
            return list(self._events)
