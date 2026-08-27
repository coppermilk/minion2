# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Build an engine's params straight from its JSON section.

Each engine used to transcribe its section by hand -- one
``float(cfg.get('latency_log_mu', 7.0))`` per field -- writing every default
TWICE, on the dataclass and again in the loader. Two sources of truth for one
number is how they drift.

Here the dataclass IS the schema: a field is read from the key of its own
name, coerced by its own annotation, and an absent (or null) key falls back
to the default the field declares. What a plain key cannot give -- a renamed
key, a pool built from the emoji catalog, a mode-dependent cadence -- comes
in as ``extra``; an annotation with no reader raises rather than silently
handing back the default.
"""

from __future__ import annotations

from dataclasses import fields
from typing import TYPE_CHECKING
from typing import TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterable
    from collections.abc import Mapping

T = TypeVar('T')


def _ints(value: object) -> frozenset[int]:
    """Read a JSON list of hours into a frozen set of ints."""
    items: Iterable[object] = value if isinstance(value, list) else []
    return frozenset(int(x) for x in items)  # type: ignore[call-overload]


def _strs(value: object) -> tuple[str, ...]:
    """Read a JSON list into a tuple of strings."""
    items: Iterable[object] = value if isinstance(value, list) else []
    return tuple(str(x) for x in items)


READERS: Mapping[str, Callable[[object], object]] = {
    'bool': bool,
    'int': int,
    'float': float,
    'str': str,
    'frozenset[int]': _ints,
    'tuple[str, ...]': _strs,
}
"""One reader per annotation a plain JSON key can fill."""


def _spelling(annotation: object) -> str:
    """Return an annotation's spelling, whether it arrived typed or as text.

    ``dataclasses`` hands back a string under ``from __future__ import
    annotations`` and the real object without it; keying the readers one way
    stops a dropped import silently reverting fields to their defaults.
    """
    if isinstance(annotation, type):
        return annotation.__name__
    return str(annotation)


def decode(
    cls: type[T],
    section: Mapping[str, object],
    extra: Mapping[str, object] | None = None,
) -> T:
    """Build ``cls`` from its JSON ``section``; ``extra`` wins over it.

    Raises ``TypeError`` for a field neither ``extra`` supplies nor
    ``READERS`` can read: a new annotation fails loudly, never silently.
    """
    given = dict(extra or {})
    for field in fields(cls):  # type: ignore[arg-type]
        if field.name in given:
            continue
        read = READERS.get(_spelling(field.type))
        if read is None:
            msg = (
                f'{cls.__name__}.{field.name}: no reader for '
                f'{field.type!r}; pass it in extra'
            )
            raise TypeError(msg)
        raw = section.get(field.name)
        if raw is not None:
            given[field.name] = read(raw)
    return cls(**given)


def section(data: Mapping[str, object], name: str) -> Mapping[str, object]:
    """Return the named JSON sub-section, or an empty one."""
    got = data.get(name)
    return got if isinstance(got, dict) else {}
