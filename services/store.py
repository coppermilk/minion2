"""Object store for the service data plane (PLATFORM.md, section 4).

A service is stateless: it fetches its input by reference, processes into a
local temp, and puts the output back, returning a reference. The Store
hides where objects live -- a local filesystem for offline/dev, an
S3-compatible bucket (MinIO or AWS) in the cloud -- behind one interface,
so the same service image runs in both.

Refs are URLs: ``file://<abs-path>`` for the local store, ``s3://<bucket>/
<key>`` for the object store. boto3 loads lazily, so importing this module
(and the hermetic local path) needs no cloud SDK.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
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
    """A Store over a local directory (offline/dev; ``file://`` refs)."""

    def __init__(self, root: Path) -> None:
        self._root = root

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


class S3Store:
    """A Store over an S3-compatible bucket (MinIO/AWS; ``s3://`` refs)."""

    def __init__(self, bucket: str, endpoint: str | None = None) -> None:
        self._bucket = bucket
        self._endpoint = endpoint

    def _client(self) -> Any:  # noqa: ANN401 -- boto3 client is untyped
        import boto3

        return boto3.client('s3', endpoint_url=self._endpoint)

    def fetch(self, ref: str, into: Path) -> Path:
        """Download an ``s3://bucket/key`` object into ``into``."""
        parsed = urlparse(ref)
        key = parsed.path.lstrip('/')
        into.mkdir(parents=True, exist_ok=True)
        dst = into / Path(key).name
        self._client().download_file(parsed.netloc, key, str(dst))
        return dst

    def put(self, key: str, src: Path) -> str:
        """Upload ``src`` to ``bucket/key``; return its ``s3://`` ref."""
        self._client().upload_file(str(src), self._bucket, key)
        return f's3://{self._bucket}/{key}'


def child_refs(store: Store, key: str, folder: Path) -> Iterator[str]:
    """Put each file under a folder result; yield their refs (frames)."""
    for path in sorted(folder.iterdir()):
        if path.is_file():
            yield store.put(f'{key}/{path.name}', path)
