"""A thread-local progress sink: an adapter reports, a caller collects.

The download adapter (fetch) knows a percent but not who wants it; a service
job wants the percent but must not reach into the adapter. This is the seam:
the caller installs a sink with ``reporting_to`` for the duration of a run,
the adapter calls ``current()`` and feeds it. A ``ContextVar`` keeps it
per-run (per thread/task), so concurrent jobs never cross progress.

Below minion_core (stdlib only), so both the adapter and the services tier
may use it without either importing the other (the import-direction law).
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterator


@dataclass(frozen=True)
class Report:
    """One progress tick: percent, bytes done/total, and ETA (seconds).

    Bytes and ETA are 0 when the source does not know them yet, so a
    consumer can always render at least the percent.
    """

    pct: int
    done_bytes: int = 0
    total_bytes: int = 0
    eta_sec: int = 0


_SINK: ContextVar[Callable[[Report], None] | None] = ContextVar(
    'progress_sink', default=None
)


def current() -> Callable[[Report], None] | None:
    """The sink installed for this run, or None when nobody is listening."""
    return _SINK.get()


@contextmanager
def reporting_to(sink: Callable[[Report], None]) -> Iterator[None]:
    """Install ``sink`` as the progress target for the wrapped run."""
    token = _SINK.set(sink)
    try:
        yield
    finally:
        _SINK.reset(token)


_DOT = chr(0xB7)  # middle dot; ASCII source, renders as a separator


def mb(num_bytes: int) -> str:
    """Bytes as megabytes with one decimal (e.g. ``47.6``)."""
    return f'{num_bytes / 1_000_000:.1f}'


def downloading_detail(report: Report) -> str:
    """The ``24.8 / 47.6 MB . 12s left`` line, or '' when nothing is known."""
    if report.total_bytes > 0:
        base = f'{mb(report.done_bytes)} / {mb(report.total_bytes)} MB'
    elif report.done_bytes > 0:
        base = f'{mb(report.done_bytes)} MB'
    else:
        return ''
    if report.eta_sec > 0:
        return f'{base} {_DOT} {report.eta_sec}s left'
    return base


def done_detail(size_bytes: int, elapsed_sec: int) -> str:
    """The ``47.6 MB . 47s`` line for a finished download."""
    return f'{mb(size_bytes)} MB {_DOT} {elapsed_sec}s'
