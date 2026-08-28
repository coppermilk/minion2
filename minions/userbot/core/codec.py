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
from typing import Any
from typing import TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Mapping

T = TypeVar('T')


def num(value: object, fallback: float = 0.0) -> float:
    """Read one JSON value as a float, ``fallback`` when it is not one.

    The five readers below are the whole reason the rest of this package
    can be type-checked. A parsed JSON value is ``object``, and code that
    calls ``float(x)`` or ``x.get(k)`` on one is asserting a shape the
    file is free not to have -- which is a crash on a typo in a config an
    operator edits by hand. Reading through here answers "what does this
    key mean if the file is wrong", once, per kind.
    """
    return float(value) if isinstance(value, (int, float)) else fallback


def whole(value: object, fallback: int = 0) -> int:
    """Read one JSON value as an int, ``fallback`` when it is not one."""
    return int(value) if isinstance(value, (int, float)) else fallback


def text(value: object, fallback: str = '') -> str:
    """Read one JSON value as a string, ``fallback`` when it is absent."""
    return str(value) if value is not None else fallback


def rows(value: object) -> list[object]:
    """Read one JSON value as an array; an empty one when it is not."""
    return list(value) if isinstance(value, list) else []


def table(value: object) -> dict[str, object]:
    """Read one JSON value as an object; an empty one when it is not."""
    return dict(value) if isinstance(value, dict) else {}


def _ints(value: object) -> frozenset[int]:
    """Read a JSON list of hours into a frozen set of ints."""
    return frozenset(whole(x) for x in rows(value))


def _strs(value: object) -> tuple[str, ...]:
    """Read a JSON list into a tuple of strings."""
    return tuple(str(x) for x in rows(value))


READERS: Mapping[str, Callable[[Any], object]] = {
    'bool': bool,
    'int': whole,
    'float': num,
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


def section(data: Mapping[str, object], *path: str) -> Mapping[str, object]:
    """Return the sub-section at ``path``, or an empty one.

    ``section(data, 'engines', 'reactions')`` walks a level at a time, so a
    missing or non-object step reads as "no settings here" rather than
    raising -- a half-written constants file must not take the bot down.
    """
    node: Mapping[str, object] = data
    for step in path:
        got = node.get(step)
        if not isinstance(got, dict):
            return {}
        node = got
    return node


def engine(data: Mapping[str, object], name: str) -> Mapping[str, object]:
    """Return one engine's settings block from ``engines.<name>``."""
    return section(data, 'engines', name)
