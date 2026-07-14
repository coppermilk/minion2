"""files adapter tests: REQ-DATA-001/002, REQ-RES-002/003, naming."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from minion_core.adapters.files import BatchLock
from minion_core.adapters.files import BudgetWriter
from minion_core.adapters.files import Deliver
from minion_core.adapters.files import QuotaExceeded
from minion_core.adapters.files import Shelve
from minion_core.adapters.files import atomic_write
from minion_core.adapters.files import free_quota
from minion_core.adapters.files import has_week
from minion_core.adapters.files import next_free_path
from minion_core.adapters.files import next_free_prim
from minion_core.adapters.files import sanitize
from minion_core.adapters.files import stem
from minion_core.adapters.files import strip_week
from minion_core.adapters.files import tag_week
from minion_core.adapters.files import usd_prim
from minion_core.kernel import Disposition
from minion_core.kernel import Envelope
from minion_core.kernel import Job
from minion_core.kernel import Origin
from minion_core.kernel import Verdict
from tests.conftest import make_cfg

if TYPE_CHECKING:
    from pathlib import Path


def test_collision_resolves_to_2(tmp_path: Path) -> None:
    """REQ-DATA-001: output never overwrites an existing file."""
    taken = tmp_path / 'a.jpg'
    taken.write_bytes(b'x')
    assert next_free_path(taken) == tmp_path / 'a_2.jpg'
    (tmp_path / 'a_2.jpg').write_bytes(b'x')
    assert next_free_path(taken) == tmp_path / 'a_3.jpg'
    assert next_free_path(tmp_path / 'free.jpg').name == 'free.jpg'


def test_prim_collision_keeps_a_valid_prim(tmp_path: Path) -> None:
    """Library collisions stay USD prims: a bare digit, no '_'."""
    taken = tmp_path / 'FgSnapeOfficeAngry.jpg'
    taken.write_bytes(b'x')
    assert next_free_prim(taken).name == 'FgSnapeOfficeAngry2.jpg'
    (tmp_path / 'FgSnapeOfficeAngry2.jpg').write_bytes(b'x')
    assert next_free_prim(taken).name == 'FgSnapeOfficeAngry3.jpg'
    assert next_free_prim(tmp_path / 'PrWand.jpg').name == 'PrWand.jpg'


def test_usd_prim_sanitizes_untrusted_names() -> None:
    """OPERATIONS 6: library names are valid USD prim identifiers."""
    assert usd_prim('FgSnapeOfficeAngry') == 'FgSnapeOfficeAngry'
    assert usd_prim('Fg Snape_Office-1!') == 'FgSnapeOffice1'
    assert usd_prim('42Wallpaper') == 'X42Wallpaper'
    assert usd_prim('') == 'Item'
    assert usd_prim('***') == 'Item'
    assert len(usd_prim('A' * 500)) <= 80


def test_shelve_folders_result_and_keeps_original(tmp_path: Path) -> None:
    """A delivered result -> <MMDD> <name>/ with the original in _done/."""
    from minion_core.adapters.tg import spool_of

    into = tmp_path / 'done'
    result = tmp_path / 'work' / 'out.jpg'
    result.parent.mkdir(parents=True)
    result.write_bytes(b'censored')
    original = tmp_path / 'spool' / 'photo.jpg'
    original.parent.mkdir(parents=True)
    original.write_bytes(b'orig')
    job = Job(
        src=original,
        dest=into,
        stem='photo',
        origin=Origin('tg', f'1:2::{original}'),
    )
    env = Envelope(job, Verdict(Disposition.DELIVERED, result=result))
    Shelve(into, spool_of).handle(env)
    folders = [p for p in into.iterdir() if p.is_dir()]
    assert len(folders) == 1
    folder = folders[0]
    assert folder.name.endswith('photo')  # MMDD photo
    assert (folder / 'out.jpg').is_file()  # the result
    assert (folder / '_done' / 'photo.jpg').is_file()  # original kept
    assert not original.exists()  # moved out of the spool


def test_shelve_ignores_a_non_delivery(tmp_path: Path) -> None:
    """A FAILED/SKIPPED verdict leaves everything untouched."""
    into = tmp_path / 'done'
    job = Job(
        src=tmp_path / 'x', dest=into, stem='x', origin=Origin('loc', 'x')
    )
    Shelve(into).handle(Envelope(job, Verdict(Disposition.FAILED)))
    assert not into.exists()


def test_fandom_tag_round_trip(tmp_path: Path) -> None:
    """The classify verdict survives the week inside the JPEG."""
    from PIL import Image

    from minion_core.adapters.files import read_fandom
    from minion_core.adapters.files import tag_fandom

    pic = tmp_path / 'FgSnapeOfficeAngry.jpg'
    Image.new('RGB', (8, 8), (10, 20, 30)).save(pic, 'JPEG')
    assert read_fandom(pic) == ''
    tag_fandom(pic, 'HarryPotter')
    assert read_fandom(pic) == 'HarryPotter'
    png = tmp_path / 'PrWand.png'
    Image.new('RGB', (8, 8)).save(png, 'PNG')
    tag_fandom(png, 'HarryPotter')  # no-op, must not raise
    assert read_fandom(png) == ''


def test_interrupted_write_leaves_no_torn_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REQ-DATA-002: a crash mid-write never tears the target."""
    target = tmp_path / 'media.bin'
    target.write_bytes(b'original')

    def explode(self: object, _dst: object) -> None:
        raise OSError('injected crash')

    monkeypatch.setattr('pathlib.Path.replace', explode)
    with pytest.raises(OSError, match='injected'):
        atomic_write(target, b'new data')
    monkeypatch.undo()
    assert target.read_bytes() == b'original'
    assert list(tmp_path.glob('*.part')) == []


def test_atomic_write_round_trip(tmp_path: Path) -> None:
    """The happy path lands the exact bytes."""
    target = tmp_path / 'state' / 'x.offset'
    atomic_write(target, b'42')
    assert target.read_bytes() == b'42'


def test_budget_writer_aborts_mid_stream(tmp_path: Path) -> None:
    """REQ-RES-002: the mid-stream check kills an oversize stream."""
    writer = BudgetWriter(tmp_path / 'big.bin', budget=10)
    writer.write(b'12345678')
    with pytest.raises(QuotaExceeded, match='quota_exceeded'):
        writer.write(b'12345678')
    assert list(tmp_path.iterdir()) == []  # no partials, no target


def test_budget_writer_commits_within_budget(tmp_path: Path) -> None:
    """Within budget the write lands atomically."""
    writer = BudgetWriter(tmp_path / 'ok.bin', budget=100)
    writer.write(b'payload')
    got = writer.commit()
    assert got.read_bytes() == b'payload'


def test_free_quota_counts_the_tree(tmp_path: Path) -> None:
    """REQ-RES-002 pre-check side: quota tracks tree bytes."""
    cfg = make_cfg(tmp_path / 'drive', QUOTA_BYTES='100')
    (cfg.inbox / 'a.bin').write_bytes(b'x' * 60)
    assert free_quota(cfg) == 40


def test_second_batch_invocation_is_locked_out(
    tmp_path: Path,
) -> None:
    """REQ-RES-003: a batch bot cannot overlap its own run."""
    path = tmp_path / 'sort.lock'
    first = BatchLock(path)
    second = BatchLock(path)
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_stale_lock_of_dead_process_is_reaped(tmp_path: Path) -> None:
    """A crash on THIS host cannot wedge the schedule."""
    import socket

    path = tmp_path / 'sort.lock'
    path.write_text(f'{socket.gethostname()}:999999999', encoding='ascii')
    lock = BatchLock(path)
    assert lock.acquire()
    lock.release()


def test_foreign_host_lock_is_never_stolen(tmp_path: Path) -> None:
    """A foreign container's lock is respected, not reaped.

    Pid liveness means nothing across pid namespaces (REQ-RES-003).
    """
    path = tmp_path / 'sort.lock'
    path.write_text('other-container:999999999', encoding='ascii')
    lock = BatchLock(path)
    assert not lock.acquire()
    assert path.exists()


def test_break_orphan_clears_a_recreated_containers_lock(
    tmp_path: Path,
) -> None:
    """A single-instance daemon self-heals a foreign-host orphan.

    ``acquire`` still respects the foreign lock; ``break_orphan``
    explicitly clears it at startup so sort is not wedged forever.
    """
    path = tmp_path / 'sort.lock'
    path.write_text('old-container:999999999', encoding='ascii')
    lock = BatchLock(path)
    assert not lock.acquire()  # foreign orphan wedges acquire
    lock.break_orphan()
    assert lock.acquire()  # ... but is cleared, so the run proceeds
    lock.release()


def test_stem_is_canonical(tmp_path: Path) -> None:
    """OPERATIONS 6: MMDD_<source>_<name>; the name is preserved."""
    when = date(2026, 7, 2)
    assert stem('cat pic!', 'tg', when) == '0702_tg_cat pic!'
    assert stem('x', 'loc', when).startswith('0702_loc_')


def test_sanitize_keeps_the_original_name() -> None:
    """Transport must never lose the sender's name (Cyrillic, spaces).

    Unicode is built from code points so this source stays ASCII
    (repo gate); runtime filenames on disk may be UTF-8.
    """
    quoted = chr(0xAB) + 'BU' + chr(0xBB)  # guillemets around BU
    assert sanitize(f'LISOVSKIY {quoted}.mp4') == f'LISOVSKIY {quoted}.mp4'
    klip = ''.join(map(chr, (0x41A, 0x43B, 0x438, 0x43F)))  # Cyrillic
    assert sanitize(f'{klip} 5.mov') == f'{klip} 5.mov'


def test_sanitize_strips_only_dangerous_chars() -> None:
    """Path separators, control and reserved chars are the only losses."""
    assert sanitize('../../etc/passwd') == 'etc_passwd'
    assert sanitize('a/b\\c:d?e') == 'a_b_c_d_e'
    assert sanitize('') == 'item'
    assert sanitize('///') == 'item'
    assert len(sanitize('x' * 500)) <= 80


def test_deliver_moves_collision_free(tmp_path: Path) -> None:
    """Deliver lands under the canonical stem, never overwriting."""
    src = tmp_path / 'incoming.JPG'
    src.write_bytes(b'img')
    dest = tmp_path / 'inbox'
    job = Job(
        src=src, dest=dest, stem='incoming', origin=Origin('loc', str(src))
    )
    verdict = Deliver().process(job)
    assert verdict.disposition is Disposition.DELIVERED
    assert verdict.result is not None
    assert verdict.result.parent == dest
    assert verdict.result.suffix == '.jpg'
    assert not src.exists()


def test_deliver_rejects_missing_input(tmp_path: Path) -> None:
    """Validation is explicit, not an assert (BLUEPRINT 4)."""
    ghost = tmp_path / 'ghost.jpg'
    job = Job(
        src=ghost,
        dest=tmp_path,
        stem='ghost',
        origin=Origin('loc', str(ghost)),
    )
    verdict = Deliver().process(job)
    assert verdict.disposition is Disposition.REJECTED
    assert verdict.reason == 'missing_input'


def _jpeg(path: Path) -> Path:
    from PIL import Image

    Image.new('RGB', (8, 8), (200, 10, 10)).save(path, 'JPEG')
    return path


def test_week_tag_round_trip(tmp_path: Path) -> None:
    """The weekly EXIF tag writes, reads, and strips cleanly."""
    pic = _jpeg(tmp_path / 'pic.jpg')
    assert not has_week(pic, 'wk')
    tag_week(pic, 'wk')
    assert has_week(pic, 'wk')
    strip_week(pic, 'wk')
    assert not has_week(pic, 'wk')
