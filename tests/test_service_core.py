"""Phase 2 core: the data plane meets the Phase 0 dispatcher.

Hermetic (LocalStore, no web stack): a stored input is fetched, the Step
runs via invoke, and the output is put back byte-for-byte. This is the
logic both the HTTP and MCP skins call, so proving it here keeps the skin
tests thin (they live in services/tests, off the kernel gate).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from minion_core.adapters.files import Deliver
from minions.svc.frames.step import ExtractFrames
from services.core import ServiceRequest
from services.core import run_service
from services.store import LocalStore

if TYPE_CHECKING:
    from pathlib import Path

    from minion_core.kernel import Stage
    from minion_core.settings import Settings


def _deliver(_cfg: Settings) -> Stage:
    return Deliver()


def _frames(cfg: Settings) -> Stage:
    return ExtractFrames(cfg)


def test_local_store_round_trips_bytes(tmp_path: Path) -> None:
    """Put then fetch returns the same bytes under a fresh name."""
    store = LocalStore(tmp_path / 'store')
    src = tmp_path / 'in' / 'a.jpg'
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b'img-bytes')
    ref = store.put('k/a.jpg', src)
    assert ref.startswith('file://')
    got = store.fetch(ref, tmp_path / 'work')
    assert got.read_bytes() == b'img-bytes'


def test_run_service_delivers_and_stores_output(tmp_path: Path) -> None:
    """Deliver over a stored input returns a delivered output ref."""
    store = LocalStore(tmp_path / 'store')
    src = tmp_path / 'in' / 'photo.jpg'
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b'hello')
    ref = store.put('inbox/photo.jpg', src)

    result = run_service(ServiceRequest('deliver', ref), store, _deliver)

    assert result.disposition == 'delivered'
    assert result.output_ref is not None
    assert result.ms >= 0.0
    out = store.fetch(result.output_ref, tmp_path / 'out')
    assert out.read_bytes() == b'hello'  # bytes preserved end to end


def test_directory_result_stores_each_file(tmp_path, monkeypatch) -> None:
    """A folder result (frames) is stored file by file into outputs."""
    from minion_core.adapters import video

    def fake_frames(src: Path, out: Path, spec: object) -> list[Path]:
        out.mkdir(parents=True, exist_ok=True)
        shots = [out / f'frame_{i:04d}.jpg' for i in range(1, 4)]
        for shot in shots:
            shot.write_bytes(b'jpg')
        return shots

    monkeypatch.setattr(video, 'frames', fake_frames)
    monkeypatch.setattr(video, 'probe_fps', lambda p, t: 5.0)

    store = LocalStore(tmp_path / 'store')
    src = tmp_path / 'clip.mp4'
    src.write_bytes(b'video')
    ref = store.put('inbox/clip.mp4', src)

    result = run_service(ServiceRequest('frames', ref), store, _frames)

    assert result.disposition == 'delivered'
    assert len(result.outputs) == 3  # one ref per frame
    assert result.output_ref is None  # many outputs, no single object
    got = store.fetch(result.outputs[0], tmp_path / 'out')
    assert got.read_bytes() == b'jpg'


def test_run_service_is_stateless(tmp_path: Path) -> None:
    """Two runs of the same input each deliver, independent of a tree."""
    store = LocalStore(tmp_path / 'store')
    src = tmp_path / 'in' / 'x.jpg'
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b'x')
    ref = store.put('inbox/x.jpg', src)
    first = run_service(ServiceRequest('deliver', ref), store, _deliver)
    second = run_service(ServiceRequest('deliver', ref), store, _deliver)
    assert first.disposition == second.disposition == 'delivered'
