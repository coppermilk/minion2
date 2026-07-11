"""Local object store for the service data plane.

A service is stateless: it fetches its input by reference, processes into a
local temp, and puts the output back, returning a reference. The Store hides
where objects live behind one interface. Refs are ``file://`` URLs, and a
fresh ephemeral store backs each request -- a service holds no state between
calls and needs no shared object store (bytes in, bytes out over the web).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Protocol
from urllib.parse import urlparse

if TYPE_CHECKING:
    from collections.abc import Iterator


class Store(Protocol):
    """Fetch an input by ref; put an output and mint its ref."""

    def fetch(self, ref: str, into: Path) -> Path:
        """Copy the referenced object into ``into``; return its path."""
        ...

    def put(self, key: str, src: Path) -> str:
        """Store the local file under ``key``; return its ref."""
        ...


class LocalStore:
    """A Store over a local directory (``file://`` refs)."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)  # coerce, so a str root never breaks `/`

    def fetch(self, ref: str, into: Path) -> Path:
        """Copy a ``file://`` object into ``into``; return its path."""
        src = Path(urlparse(ref).path)
        into.mkdir(parents=True, exist_ok=True)
        dst = into / src.name
        shutil.copy2(src, dst)
        return dst

    def put(self, key: str, src: Path) -> str:
        """Copy ``src`` under ``root/key``; return its ``file://`` ref."""
        dst = self._root / key
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return f'file://{dst}'


def child_refs(store: Store, key: str, folder: Path) -> Iterator[str]:
    """Put each file under a folder result; yield their refs (frames)."""
    for path in sorted(folder.iterdir()):
        if path.is_file():
            yield store.put(f'{key}/{path.name}', path)
