"""Port requirement tests (v1 gaps folded into BLUEPRINT 3).

Covers REQ-KRN-005 (write stability), REQ-PRT-001 (spooler axis),
REQ-DOCK-001 (two docks, one belt) and REQ-CATCH-001/002 (catch).
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from minion_core.kernel import ArchiveTo
from minion_core.kernel import Disposition
from minion_core.kernel import Envelope
from minion_core.kernel import Folder
from minion_core.kernel import FolderSpec
from minion_core.kernel import Job
from minion_core.kernel import Origin
from minion_core.kernel import RouteOrigin
from minion_core.kernel import SeenPaths
from minion_core.kernel import SendResult
from minion_core.kernel import Step
from minion_core.kernel import Verdict
from minions.catch.main import CatchDeps
from minions.catch.main import ClassifyCopy
from minions.catch.main import build as build_catch
from minions.censor.main import build as build_censor
from minions.print.main import PrintPdf
from tests.conftest import make_cfg

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from minion_core.kernel import Stage
    from minion_core.settings import Settings


# ---------------------------------------------------- REQ-KRN-005


def test_growing_file_is_withheld_until_stable(tmp_path: Path) -> None:
    """REQ-KRN-005: a file mid-copy is never consumed torn."""
    root = tmp_path / 'watch'
    root.mkdir()
    grower = root / 'big.jpg'
    grower.write_bytes(b'x' * 10)
    spec = FolderSpec(root=root, dest=tmp_path, exts=('.jpg',))
    folder = Folder(spec, SeenPaths(8))
    emitted: list[Envelope] = []

    folder._scan(emitted.append)  # first sight: pending
    assert emitted == []
    grower.write_bytes(b'x' * 20)  # still being written
    folder._scan(emitted.append)  # size changed: still pending
    assert emitted == []
    folder._scan(emitted.append)  # stable now: emitted
    assert [e.job.src.name for e in emitted] == ['big.jpg']
    folder._scan(emitted.append)  # dedup: exactly once
    assert len(emitted) == 1


def test_once_mode_emits_stable_files(tmp_path: Path) -> None:
    """Once-mode runs the sight and stability scans back to back."""
    root = tmp_path / 'watch'
    root.mkdir()
    (root / 'a.pdf').write_bytes(b'pdf')
    spec = FolderSpec(root=root, dest=tmp_path, exts=('.pdf',), once=True)
    out = list(Folder(spec, SeenPaths(8))(iter(())))
    assert [e.job.src.name for e in out] == ['a.pdf']


# ---------------------------------------------------- REQ-PRT-001


def test_spooler_argv_comes_from_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REQ-PRT-001: the spooler is configuration, not code."""
    cfg = make_cfg(
        tmp_path / 'drive',
        PRINT_SPOOLER='/apps/SumatraPDF.exe;-print-to-default;-silent',
    )
    seen_argv: list[list[str]] = []

    def fake_run(argv: list[str], **kw: object) -> object:
        seen_argv.append(argv)
        return subprocess.CompletedProcess(argv, 0, b'', b'')

    monkeypatch.setattr(subprocess, 'run', fake_run)
    pdf = cfg.print_queue / 'doc.pdf'
    pdf.write_bytes(b'pdf')
    job = Job(
        src=pdf,
        dest=cfg.print_done,
        stem='doc',
        origin=Origin('loc', str(pdf)),
    )
    verdict = PrintPdf(cfg).process(job)
    assert verdict.disposition is Disposition.DELIVERED
    assert seen_argv == [
        ['/apps/SumatraPDF.exe', '-print-to-default', '-silent', str(pdf)]
    ]


def test_spooler_default_stays_lp(tmp_path: Path) -> None:
    """The NAS deployment is byte-identical: default ('lp',)."""
    cfg = make_cfg(tmp_path / 'drive')
    assert cfg.print_spooler == ('lp',)
    assert cfg.print_timeout_sec == 120


# --------------------------------------------------- REQ-DOCK-001


class _ChannelDouble:
    """Channel double: records sends."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.files: list[Path] = []

    def send_text(self, origin: Origin, text: str) -> None:
        self.texts.append(text)

    def send_file(self, origin: Origin, path: Path) -> None:
        self.files.append(path)


class _DeliverSame(Step):
    """Step double: delivers the input unchanged."""

    def process(self, job: Job) -> Verdict:
        return Verdict(Disposition.DELIVERED, result=job.src)


class _Fixed(Step):
    """Head double: __call__ ignores upstream, emits fixed envs."""

    def __init__(self, envs: list[Envelope]) -> None:
        self._envs = envs

    def __call__(self, _up: object) -> object:  # type: ignore[override]
        return iter(self._envs)

    def process(self, job: Job) -> Verdict:
        raise NotImplementedError


def _env_for(path: Path, source: str) -> Envelope:
    origin = Origin(source=source, ref=str(path))
    return Envelope(
        Job(src=path, dest=path.parent, stem=path.stem, origin=origin)
    )


def test_route_origin_serves_both_docks(tmp_path: Path) -> None:
    """REQ-DOCK-001: tg -> chat, loc -> done dir, no cross-talk."""
    tg_file = tmp_path / 'from_chat.jpg'
    tg_file.write_bytes(b'tg')
    loc_file = tmp_path / 'from_watch.jpg'
    loc_file.write_bytes(b'loc')
    done = tmp_path / 'done'
    channel = _ChannelDouble()
    graph: Stage = (
        _Fixed([_env_for(tg_file, 'tg'), _env_for(loc_file, 'loc')])
        >> _DeliverSame()
        >> RouteOrigin(tg=SendResult(channel), loc=ArchiveTo(done))
    )
    list(graph(iter(())))
    assert channel.files == [tg_file]  # tg reached the chat only
    assert (done / 'from_watch.jpg').exists()  # loc reached done only
    assert tg_file.exists()  # ArchiveTo never touched the tg job


def test_censor_build_merges_watch_dock(tmp_path: Path) -> None:
    """The merged, tokenless censor graph still assembles."""
    watch = tmp_path / 'censor_watch'
    watch.mkdir()
    cfg = make_cfg(tmp_path / 'drive', CENSOR_WATCH=str(watch))
    assert cfg.censor_watch == watch
    assert build_censor(cfg, {'TG_TOKEN': ''}) is not None


# ------------------------------------------------- REQ-CATCH-001/2


def _jpeg(path: Path) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (8, 8), (30, 30, 220)).save(path, 'JPEG')
    return path


def _deps(*, namer_fails: bool = False) -> CatchDeps:
    import numpy as np

    def namer(path: Path) -> str:
        if namer_fails:
            raise RuntimeError('llm down')
        return f'label {path.stem}'

    return CatchDeps(namer=namer, embed=lambda p: np.array([1.0, 0.0]))


def _catch_job(cfg: Settings, path: Path) -> Job:
    return Job(
        src=path,
        dest=cfg.pictures,
        stem=path.stem,
        origin=Origin('loc', str(path)),
    )


def test_catch_copies_and_never_moves(tmp_path: Path) -> None:
    """REQ-CATCH-001: the original stays; the copy is identical."""
    downloads = tmp_path / 'Downloads'
    cfg = make_cfg(tmp_path / 'drive', CATCH_DIR=str(downloads))
    _jpeg(cfg.pictures / 'Cats' / 'cat_0.jpg')
    src = _jpeg(downloads / 'wallpaper.jpg')
    original_bytes = src.read_bytes()

    verdict = ClassifyCopy(cfg, _deps()).process(_catch_job(cfg, src))
    assert verdict.disposition is Disposition.DELIVERED
    assert verdict.result is not None
    assert verdict.result.parent == cfg.pictures / 'Cats'
    assert verdict.result.read_bytes() == original_bytes
    stayed = list(downloads.iterdir())  # renamed, never relocated
    assert len(stayed) == 1
    assert stayed[0].read_bytes() == original_bytes
    assert 'label_wallpaper' in stayed[0].name


def test_catch_failure_leaves_file_untouched(tmp_path: Path) -> None:
    """REQ-CATCH-002: a namer crash is FAILED; the belt continues."""
    downloads = tmp_path / 'Downloads'
    cfg = make_cfg(tmp_path / 'drive', CATCH_DIR=str(downloads))
    first = _jpeg(downloads / 'one.jpg')
    before = first.read_bytes()
    step = ClassifyCopy(cfg, _deps(namer_fails=True))

    verdict = step.process(_catch_job(cfg, first))
    assert verdict.disposition is Disposition.FAILED
    assert verdict.reason == 'classify_failed'
    assert first.exists()
    assert first.read_bytes() == before

    ok = ClassifyCopy(cfg, _deps())  # the next file still processes
    second = _jpeg(downloads / 'two.jpg')
    assert (
        ok.process(_catch_job(cfg, second)).disposition
        is Disposition.DELIVERED
    )


def test_catch_skips_already_labelled(tmp_path: Path) -> None:
    """A renamed original is never re-caught into a loop."""
    downloads = tmp_path / 'Downloads'
    cfg = make_cfg(tmp_path / 'drive', CATCH_DIR=str(downloads))
    done = _jpeg(downloads / '0702_loc_label_wallpaper.jpg')
    verdict = ClassifyCopy(cfg, _deps()).process(_catch_job(cfg, done))
    assert verdict.disposition is Disposition.SKIPPED
    assert verdict.reason == 'already_labelled'


def test_catch_rejects_non_image(tmp_path: Path) -> None:
    """Untrusted bytes are validated explicitly (BLUEPRINT 4)."""
    downloads = tmp_path / 'Downloads'
    cfg = make_cfg(tmp_path / 'drive', CATCH_DIR=str(downloads))
    downloads.mkdir(parents=True, exist_ok=True)
    fake = downloads / 'evil.jpg'
    fake.write_bytes(b'not an image')
    verdict = ClassifyCopy(cfg, _deps()).process(_catch_job(cfg, fake))
    assert verdict.disposition is Disposition.REJECTED
    assert verdict.reason == 'bad_image'
    assert fake.exists()


def test_catch_end_to_end_over_the_belt(tmp_path: Path) -> None:
    """The whole graph: once-dock -> classify -> copy."""
    downloads = tmp_path / 'Downloads'
    cfg = make_cfg(tmp_path / 'drive', CATCH_DIR=str(downloads))
    _jpeg(cfg.pictures / 'Cats' / 'cat_0.jpg')
    _jpeg(downloads / 'new.jpg')
    assert cfg.catch_dir is not None
    spec = FolderSpec(
        root=cfg.catch_dir, dest=cfg.pictures, exts=('.jpg',), once=True
    )
    graph = Folder(spec, SeenPaths(8)) >> ClassifyCopy(cfg, _deps())
    out = list(graph(iter(())))
    assert len(out) == 1
    assert out[0].verdict is not None
    assert out[0].verdict.disposition is Disposition.DELIVERED
    assert build_catch(cfg, _deps()) is not None
