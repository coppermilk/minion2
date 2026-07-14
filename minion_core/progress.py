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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterator

_SINK: ContextVar[Callable[[int], None] | None] = ContextVar(
    'progress_sink', default=None
)


def current() -> Callable[[int], None] | None:
    """The sink installed for this run, or None when nobody is listening."""
    return _SINK.get()


@contextmanager
def reporting_to(sink: Callable[[int], None]) -> Iterator[None]:
    """Install ``sink`` as the progress target for the wrapped run."""
    token = _SINK.set(sink)
    try:
        yield
    finally:
        _SINK.reset(token)
