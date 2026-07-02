"""sort bot tests: the four passes and REQ-SORT-001 ordering."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from minion_core.adapters.vision import EmbeddingCache
from minions.sort.passes import SortDeps
from minions.sort.passes import demote_pass
from minions.sort.passes import name_pass
from minions.sort.passes import place_pass
from minions.sort.passes import replace_pass
from minions.sort.passes import run_passes
from tests.conftest import make_cfg

if TYPE_CHECKING:
    from pathlib import Path

    from minion_core.adapters.vision import Vector
    from minion_core.settings import Settings

CAT = np.array([1.0, 0.0])
DOG = np.array([0.0, 1.0])


def _embed(path: Path) -> Vector:
    return CAT if 'cat' in path.name.lower() else DOG


DEPS = SortDeps(namer=lambda p: f'named {p.stem}', embed=_embed)


def _jpeg(path: Path) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (8, 8), (10, 200, 10)).save(path, 'JPEG')
    return path


def _seed_library(cfg: Settings) -> None:
    for i in range(3):
        _jpeg(cfg.pictures / 'Cats' / f'cat_{i}.jpg')
        _jpeg(cfg.pictures / 'Dogs' / f'dog_{i}.jpg')


def test_name_pass_renames_via_llm_label(tmp_path: Path) -> None:
    """Pass 1: the label becomes the canonical stem."""
    cfg = make_cfg(tmp_path / 'drive')
    _jpeg(cfg.inbox / 'IMG_0001.jpg')
    name_pass(cfg, DEPS)
    names = [p.name for p in cfg.inbox.iterdir()]
    assert len(names) == 1
    assert 'named_IMG_0001' in names[0]
    assert names[0].split('_')[1] == 'loc'


def test_name_pass_rejects_non_images(tmp_path: Path) -> None:
    """Untrusted bytes are validated explicitly (BLUEPRINT 4)."""
    cfg = make_cfg(tmp_path / 'drive')
    fake = cfg.inbox / 'evil.jpg'
    fake.write_bytes(b'not an image at all')
    name_pass(cfg, DEPS)
    assert fake.exists()  # left in place, never renamed


def test_place_pass_sorts_by_nearest_fandom(tmp_path: Path) -> None:
    """Pass 2: vision picks the folder; the weekly tag rides along."""
    from minion_core.adapters.files import has_week

    cfg = make_cfg(tmp_path / 'drive')
    _seed_library(cfg)
    _jpeg(cfg.inbox / 'new_cat.jpg')
    place_pass(cfg, DEPS, EmbeddingCache(cfg))
    placed = cfg.pictures / 'Cats' / 'new_cat.jpg'
    assert placed.exists()
    assert has_week(placed, cfg.week_tag)


def test_demote_pass_moves_sparse_to_unknown(tmp_path: Path) -> None:
    """Pass 3: fandoms under demote_min_count sink to Unknown."""
    cfg = make_cfg(tmp_path / 'drive', DEMOTE_MIN_COUNT='3')
    _seed_library(cfg)
    _jpeg(cfg.pictures / 'Sparse' / 'one_cat.jpg')
    demote_pass(cfg)
    assert not (cfg.pictures / 'Sparse').exists()
    assert (cfg.pictures / 'Unknown' / 'one_cat.jpg').exists()
    assert (cfg.pictures / 'Cats').exists()  # big fandoms stay


def test_demote_then_replace_reuses_vectors(tmp_path: Path) -> None:
    """REQ-SORT-001: re-place is exact and recomputes nothing.

    Demote moves a fandom away; Re-place matches the live layout;
    every library vector is embedded exactly once in its life.
    """
    cfg = make_cfg(tmp_path / 'drive')
    _seed_library(cfg)
    _jpeg(cfg.pictures / 'Sparse' / 'lone_cat.jpg')
    _jpeg(cfg.inbox / 'fresh_cat.jpg')  # a non-idle run
    calls: list[str] = []

    def counting(path: Path) -> Vector:
        calls.append(path.name)
        return _embed(path)

    deps = SortDeps(namer=lambda p: f'named {p.stem}', embed=counting)
    run_passes(cfg, deps)
    assert not (cfg.pictures / 'Sparse').exists()  # demoted
    cats = [p.name for p in (cfg.pictures / 'Cats').iterdir()]
    assert 'lone_cat.jpg' in cats  # rescued by re-place
    library = [f'{k}_{i}.jpg' for k in ('cat', 'dog') for i in range(3)]
    for name in library:
        assert calls.count(name) == 1  # embedded once in its life


def test_replace_pass_rescues_unknown(tmp_path: Path) -> None:
    """Pass 4: Unknown re-matches against the new layout."""
    cfg = make_cfg(tmp_path / 'drive')
    _seed_library(cfg)
    _jpeg(cfg.pictures / 'Unknown' / 'lost_cat.jpg')
    cache = EmbeddingCache(cfg)
    cache.invalidate()
    replace_pass(cfg, DEPS, cache)
    assert (cfg.pictures / 'Cats' / 'lost_cat.jpg').exists()


def test_full_run_end_to_end(tmp_path: Path) -> None:
    """The four passes compose: inbox image lands in its fandom."""
    cfg = make_cfg(tmp_path / 'drive')
    _seed_library(cfg)
    _jpeg(cfg.inbox / 'stray cat.jpg')
    run_passes(cfg, DEPS)
    cats = [p.name for p in (cfg.pictures / 'Cats').iterdir()]
    assert any('stray_cat' in n for n in cats)
    assert list(cfg.inbox.iterdir()) == []


def test_idle_run_exits_fast_and_writes_nothing(tmp_path: Path) -> None:
    """OPERATIONS 5: an idle run touches neither cache nor adapters."""
    cfg = make_cfg(tmp_path / 'drive')
    _seed_library(cfg)

    def explode(path: Path) -> str:
        raise AssertionError('idle run must not call adapters')

    run_passes(cfg, SortDeps(namer=explode, embed=explode))
    assert not (cfg.regen / '_embeddings.npz').exists()


def test_source_dirs_axis(tmp_path: Path) -> None:
    """The Downloads axis: SOURCE_DIRS overrides _inbox."""
    downloads = tmp_path / 'Downloads'
    downloads.mkdir()
    cfg = make_cfg(tmp_path / 'drive', SOURCE_DIRS=str(downloads))
    _seed_library(cfg)
    _jpeg(downloads / 'dl_cat.jpg')
    run_passes(cfg, DEPS)
    assert not (downloads / 'dl_cat.jpg').exists()
    cats = [p.name for p in (cfg.pictures / 'Cats').iterdir()]
    assert any('dl_cat' in n for n in cats)


def test_sort_watch_axis_coerces(tmp_path: Path) -> None:
    """SORT_WATCH=1 enables the watch daemon; default stays off."""
    from minion_core.settings import load

    assert not load({'DRIVE': str(tmp_path)}).sort_watch
    assert load({'DRIVE': str(tmp_path), 'SORT_WATCH': '1'}).sort_watch


def test_watch_belt_sorts_on_arrival(tmp_path: Path) -> None:
    """Instant sorting: a new stable image triggers a full run."""
    from minion_core.kernel import Disposition
    from minion_core.kernel import Folder
    from minion_core.kernel import FolderSpec
    from minion_core.kernel import SeenPaths
    from minions.sort.main import SortTrigger

    cfg = make_cfg(tmp_path / 'drive')
    _seed_library(cfg)
    _jpeg(cfg.inbox / 'wild_cat.jpg')
    spec = FolderSpec(
        root=cfg.inbox, dest=cfg.inbox, exts=('.jpg',), once=True
    )
    graph = Folder(spec, SeenPaths(8)) >> SortTrigger(cfg, DEPS)
    out = list(graph(iter(())))
    assert len(out) == 1
    assert out[0].verdict is not None
    assert out[0].verdict.disposition is Disposition.DELIVERED
    cats = [p.name for p in (cfg.pictures / 'Cats').iterdir()]
    assert any('wild_cat' in n for n in cats)


def test_watch_trigger_respects_lock(tmp_path: Path) -> None:
    """A held lock turns the trigger into SKIPPED batch_locked."""
    from minion_core.adapters.files import BatchLock
    from minion_core.kernel import Disposition
    from minion_core.kernel import Job
    from minion_core.kernel import Origin
    from minions.sort.main import SortTrigger

    cfg = make_cfg(tmp_path / 'drive')
    pic = _jpeg(cfg.inbox / 'busy_cat.jpg')
    lock = BatchLock(cfg.state / 'sort.lock')
    assert lock.acquire()
    try:
        job = Job(
            src=pic,
            dest=cfg.inbox,
            stem='busy_cat',
            origin=Origin('loc', str(pic)),
        )
        verdict = SortTrigger(cfg, DEPS).process(job)
    finally:
        lock.release()
    assert verdict.disposition is Disposition.SKIPPED
    assert verdict.reason == 'batch_locked'
    assert pic.exists()  # nothing ran, nothing moved


def test_build_watch_covers_all_source_dirs(tmp_path: Path) -> None:
    """One dock per source dir, folded into one belt."""
    from minions.sort.main import build_watch

    a = tmp_path / 'a'
    b = tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    cfg = make_cfg(tmp_path / 'drive', SOURCE_DIRS=f'{a};{b}')
    assert build_watch(cfg, DEPS) is not None
