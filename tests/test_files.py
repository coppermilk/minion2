"""files adapter tests: REQ-DATA-001/002, REQ-RES-002/003, naming."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from minion_core.adapters.files import BatchLock
from minion_core.adapters.files import BudgetWriter
from minion_core.adapters.files import Deliver
from minion_core.adapters.files import QuotaExceeded
from minion_core.adapters.files import atomic_write
from minion_core.adapters.files import free_quota
from minion_core.adapters.files import has_week
from minion_core.adapters.files import next_free_path
from minion_core.adapters.files import sanitize
from minion_core.adapters.files import stem
from minion_core.adapters.files import strip_week
from minion_core.adapters.files import tag_week
from minion_core.kernel import Disposition
from minion_core.kernel import Job
from minion_core.kernel import Origin
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


def test_stem_is_canonical(tmp_path: Path) -> None:
    """OPERATIONS 6: MMDD_<source>_<name>."""
    when = date(2026, 7, 2)
    assert stem('cat pic!', 'tg', when) == '0702_tg_cat_pic'
    assert stem('x', 'loc', when).startswith('0702_loc_')


def test_sanitize_reduces_hostile_names() -> None:
    """Untrusted names collapse to a safe ASCII fragment."""
    assert sanitize('../../etc/passwd') == 'etc_passwd'
    assert sanitize('') == 'item'
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
