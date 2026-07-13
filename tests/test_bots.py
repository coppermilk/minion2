"""Bot graph tests: REQ-DEG-001 and per-bot behaviour with doubles."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import minions.bots.inbox.main
import minions.bots.week_clean.main
from minion_core.adapters.files import HideSpec
from minion_core.adapters.files import hide_boxes
from minion_core.kernel import Disposition
from minion_core.kernel import Folder
from minion_core.kernel import FolderSpec
from minion_core.kernel import Job
from minion_core.kernel import Origin
from minion_core.kernel import SeenPaths
from minions.bots.inbox.main import build as build_inbox
from minions.bots.print.main import PrintPdf
from minions.bots.print.main import build as build_print
from tests.conftest import make_cfg
from tests.conftest import make_env

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_tokenless_inbox_graph_runs(tmp_path: Path) -> None:
    """REQ-DEG-001: a token-less bot runs with zero code branches."""
    make_cfg(tmp_path / 'drive')
    env = make_env(tmp_path / 'drive')
    code = minions.bots.inbox.main.main(env)
    assert code == 0


def test_inbox_graph_shape(tmp_path: Path) -> None:
    """The graph assembles from Settings alone."""
    cfg = make_cfg(tmp_path / 'drive')
    graph = build_inbox(cfg, {'TG_TOKEN': '', 'TG_CHATS': '1'})
    assert list(graph(iter(()))) == []  # tokenless: drains clean


def test_folder_once_feeds_a_belt(tmp_path: Path) -> None:
    """Folder in once-mode is the batch dock for tests."""
    root = tmp_path / 'watch'
    root.mkdir()
    (root / 'a.pdf').write_bytes(b'pdf')
    (root / 'skip.txt').write_bytes(b'no')
    spec = FolderSpec(root=root, dest=tmp_path, exts=('.pdf',), once=True)
    out = list(Folder(spec, SeenPaths(8))(iter(())))
    assert [e.job.src.name for e in out] == ['a.pdf']


def test_print_bot_prints_and_archives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PDF -> lp -> print/_done, collision-free."""
    cfg = make_cfg(tmp_path / 'drive')
    sent: list[str] = []

    def fake_run(argv: list[str], **kw: object) -> object:
        sent.append(argv[1])
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
    assert sent == [str(pdf)]


def test_print_bot_missing_printer_is_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No lp binary is a FAILED verdict, never a crash."""
    cfg = make_cfg(tmp_path / 'drive')

    def no_lp(*a: object, **kw: object) -> object:
        raise FileNotFoundError('lp')

    monkeypatch.setattr(subprocess, 'run', no_lp)
    pdf = cfg.print_queue / 'doc.pdf'
    pdf.write_bytes(b'pdf')
    job = Job(
        src=pdf,
        dest=cfg.print_done,
        stem='doc',
        origin=Origin('loc', str(pdf)),
    )
    verdict = PrintPdf(cfg).process(job)
    assert verdict.disposition is Disposition.FAILED
    assert verdict.reason == 'printer_missing'
    assert build_print(cfg) is not None


def _jpeg(path: Path) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (32, 32), (250, 250, 250)).save(path, 'JPEG')
    return path


def test_hide_boxes_black_mode_blacks_out(tmp_path: Path) -> None:
    """Censor CT-B: the hidden region really is hidden."""
    from PIL import Image

    src = _jpeg(tmp_path / 'people.jpg')
    out = tmp_path / 'people_s1.jpg'
    hide_boxes(src, out, HideSpec(boxes=((8, 8, 24, 24),), mode='black'))
    with Image.open(out) as img:
        assert img.getpixel((16, 16)) == (0, 0, 0)
        corner = img.getpixel((2, 2))
    assert all(c > 200 for c in corner)  # outside stays bright


def test_hide_boxes_blur_mode_smears(tmp_path: Path) -> None:
    """Blur mode changes the region without blacking it."""
    from PIL import Image
    from PIL import ImageDraw

    src = tmp_path / 'detail.jpg'
    base = Image.new('RGB', (32, 32), (255, 255, 255))
    ImageDraw.Draw(base).rectangle((12, 12, 20, 20), fill=(0, 0, 0))
    base.save(src, 'JPEG')
    out = tmp_path / 'detail_s1.jpg'
    hide_boxes(src, out, HideSpec(boxes=((8, 8, 24, 24),), mode='blur'))
    with Image.open(out) as img:
        center = img.getpixel((16, 16))
    assert center != (0, 0, 0)  # no longer pure detail


def test_week_clean_untags_and_shelves(tmp_path: Path) -> None:
    """week-clean is mechanical: strip the week tag, move per EXIF.

    Every decision was made during the week (prim name, EXIF fandom,
    week tag); Monday only executes them. An unclassified leftover
    STAYS for the next attempt (never deleted).
    """
    from minion_core.adapters.files import has_week
    from minion_core.adapters.files import tag_fandom
    from minion_core.adapters.files import tag_week

    drive = tmp_path / 'drive'
    cfg = make_cfg(drive)
    week = _jpeg(cfg.inbox / 'FgSnapeOfficeAngry.jpg')
    tag_fandom(week, 'HarryPotter')
    tag_week(week, cfg.week_tag)
    (cfg.inbox / 'leftover.jpg').write_bytes(b'x')
    code = minions.bots.week_clean.main.main(make_env(drive))
    assert code == 0
    shelved = cfg.pictures / 'HarryPotter' / 'FgSnapeOfficeAngry.jpg'
    assert shelved.exists()
    assert not has_week(shelved, cfg.week_tag)  # tag ends with the week
    assert not week.exists()
    assert (cfg.inbox / 'leftover.jpg').exists()  # retried, not deleted


def test_week_clean_untagged_prim_goes_to_unknown(tmp_path: Path) -> None:
    """An image that lost its EXIF fandom is shelved into Unknown."""
    drive = tmp_path / 'drive'
    cfg = make_cfg(drive)
    _jpeg(cfg.inbox / 'PrWand.jpg')  # prim-named, no fandom tag
    assert minions.bots.week_clean.main.main(make_env(drive)) == 0
    assert (cfg.pictures / 'Unknown' / 'PrWand.jpg').exists()


def test_week_clean_respects_batch_lock(tmp_path: Path) -> None:
    """REQ-RES-003 at bot level: a held lock skips the run."""
    from minion_core.adapters.files import BatchLock

    drive = tmp_path / 'drive'
    cfg = make_cfg(drive)
    (cfg.inbox / 'stay.jpg').write_bytes(b'x')
    lock = BatchLock(cfg.state / 'week-clean.lock')
    assert lock.acquire()
    try:
        code = minions.bots.week_clean.main.main(make_env(drive))
    finally:
        lock.release()
    assert code == 0
    assert (cfg.inbox / 'stay.jpg').exists()  # nothing ran
