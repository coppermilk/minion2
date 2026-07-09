"""S3Store against a mocked S3 (moto): the object-store data plane.

Services tier. moto stands in for MinIO/AWS so this stays hermetic; a live
MinIO smoke is run out of band (see services/README.md). We check the ref
format, that the bucket is auto-created, and that bytes round-trip.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import boto3
from moto import mock_aws

from services.store import S3Store

if TYPE_CHECKING:
    from pathlib import Path

REGION = 'us-east-1'


@mock_aws
def test_put_creates_bucket_and_returns_ref(tmp_path: Path) -> None:
    """Put auto-creates the bucket and mints an s3:// ref."""
    src = tmp_path / 'photo.jpg'
    src.write_bytes(b'hello-s3')
    store = S3Store(bucket='minion', region=REGION)
    ref = store.put('deliver/photo.jpg', src)
    assert ref == 's3://minion/deliver/photo.jpg'
    # the object really landed in the bucket
    body = (
        boto3.client('s3', region_name=REGION)
        .get_object(Bucket='minion', Key='deliver/photo.jpg')['Body']
        .read()
    )
    assert body == b'hello-s3'


@mock_aws
def test_round_trips_bytes(tmp_path: Path) -> None:
    """Fetch after put returns the same bytes under a fresh name."""
    src = tmp_path / 'in' / 'a.jpg'
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b'round-trip')
    store = S3Store(bucket='minion', region=REGION)
    ref = store.put('inbox/a.jpg', src)
    got = store.fetch(ref, tmp_path / 'work')
    assert got.read_bytes() == b'round-trip'
