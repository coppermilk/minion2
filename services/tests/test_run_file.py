"""The n8n-facing /run-file: bytes in, bytes out.

Services tier. n8n's HTTP Request node has the media as binary and wants
binary back; /run-file wraps the ref-based core so it is a single node, no
S3 node. We check a file round-trips (deliver), a real blur comes back
(censor-blur, segmentation stubbed so the PIL blur is genuine but torch is
not needed), and a skip surfaces as 422.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from services.http import create_app
from services.store import LocalStore


def _sharp_jpeg(path: Path) -> Path:
    from PIL import Image

    img = Image.new('RGB', (64, 64), (255, 255, 255))
    for x in range(24, 40):
        for y in range(24, 40):
            img.putpixel((x, y), (0, 0, 0))
    img.save(path, 'JPEG')
    return path


def test_run_file_round_trips_a_file(tmp_path: Path) -> None:
    client = TestClient(create_app('deliver', LocalStore(tmp_path / 's')))
    reply = client.post(
        '/run-file',
        files={'file': ('a.bin', b'hello', 'application/octet-stream')},
    )
    assert reply.status_code == 200
    assert reply.content == b'hello'  # deliver just relocates the bytes
    assert reply.headers['x-disposition'] == 'delivered'
    assert float(reply.headers['x-run-ms']) >= 0.0


def test_run_file_blurs_via_the_censor_blur_service(
    tmp_path: Path, monkeypatch
) -> None:
    from PIL import Image

    from minion_core.adapters import vision
    from minion_core.adapters.files import Mask

    def fake_masks(path: Path) -> Mask:
        with Image.open(path) as image:
            width, height = image.size
        data = bytes(
            255 if 16 <= k % width < 48 and 16 <= k // width < 48 else 0
            for k in range(width * height)
        )
        return Mask(width=width, height=height, data=data)

    monkeypatch.setattr(vision, 'person_masks', fake_masks)
    src = _sharp_jpeg(tmp_path / 'p.jpg')
    client = TestClient(create_app('censor-blur', LocalStore(tmp_path / 's')))

    reply = client.post(
        '/run-file', files={'file': ('p.jpg', src.read_bytes(), 'image/jpeg')}
    )
    assert reply.status_code == 200
    assert reply.headers['x-disposition'] == 'delivered'
    out = tmp_path / 'out.jpg'
    out.write_bytes(reply.content)
    with Image.open(out) as result:
        assert result.size == (64, 64)
        center = result.getpixel((32, 32))
    assert center not in {(0, 0, 0), (255, 255, 255)}  # the edge got blurred


def test_run_file_422_when_the_step_skips(tmp_path: Path, monkeypatch) -> None:
    from minion_core.adapters import vision

    monkeypatch.setattr(vision, 'person_masks', lambda _p: None)
    src = _sharp_jpeg(tmp_path / 'p.jpg')
    client = TestClient(create_app('censor-blur', LocalStore(tmp_path / 's')))
    reply = client.post(
        '/run-file', files={'file': ('p.jpg', src.read_bytes(), 'image/jpeg')}
    )
    assert reply.status_code == 422
    assert 'no_person' in reply.json()['detail']  # skipped: no_person
