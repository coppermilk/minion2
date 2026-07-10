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

import os
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


class S3Store:
    """A Store over an S3-compatible bucket (MinIO/AWS; ``s3://`` refs).

    MinIO with a custom endpoint needs path-style addressing and SigV4;
    credentials come from the environment (AWS_ACCESS_KEY_ID/SECRET, the
    MinIO root user/password). The bucket is created on first ``put``.
    """

    def __init__(
        self,
        bucket: str,
        endpoint: str | None = None,
        region: str = 'us-east-1',
    ) -> None:
        self._bucket = bucket
        self._endpoint = endpoint
        self._region = region
        self._cached: Any = None

    def _client(self) -> Any:  # noqa: ANN401 -- boto3 client is untyped
        if self._cached is None:
            import boto3
            from botocore.client import Config

            self._cached = boto3.client(
                's3',
                endpoint_url=self._endpoint,
                region_name=self._region,
                config=Config(
                    signature_version='s3v4',
                    s3={'addressing_style': 'path'},
                ),
            )
        return self._cached

    def _ensure_bucket(self) -> None:
        """Create the bucket if it is missing (idempotent)."""
        from botocore.exceptions import ClientError

        client = self._client()
        try:
            client.head_bucket(Bucket=self._bucket)
        except ClientError:
            client.create_bucket(Bucket=self._bucket)

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
        self._ensure_bucket()
        self._client().upload_file(str(src), self._bucket, key)
        return f's3://{self._bucket}/{key}'


def store_from_env() -> Store:
    """Pick the Store backend from env: LocalStore (default) or S3Store.

    ``STORE_BACKEND=s3`` selects the object store (MinIO/AWS) via
    ``S3_ENDPOINT`` / ``S3_BUCKET`` / ``S3_REGION``; anything else is the
    local filesystem at ``STORE_ROOT``. One image runs both, by env only.
    """
    if os.environ.get('STORE_BACKEND') == 's3':
        return S3Store(
            bucket=os.environ.get('S3_BUCKET', 'minion'),
            endpoint=os.environ.get('S3_ENDPOINT') or None,
            region=os.environ.get('S3_REGION', 'us-east-1'),
        )
    return LocalStore(Path(os.environ.get('STORE_ROOT', '/data/store')))


def child_refs(store: Store, key: str, folder: Path) -> Iterator[str]:
    """Put each file under a folder result; yield their refs (frames)."""
    for path in sorted(folder.iterdir()):
        if path.is_file():
            yield store.put(f'{key}/{path.name}', path)
