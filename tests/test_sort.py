"""sort bot tests: the three passes and REQ-SORT-001 ordering."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from minion_core.adapters.llm import Classification
from minion_core.adapters.llm import LlmError
from minion_core.adapters.vision import EmbeddingCache
from minions.sort.passes import SortDeps
from minions.sort.passes import classify_pass
from minions.sort.passes import demote_pass
from minions.sort.passes import replace_pass
from minions.sort.passes import run_passes
from tests.conftest import make_cfg

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from minion_core.adapters.vision import Vector
    from minion_core.settings import Settings

CAT = np.array([1.0, 0.0])
DOG = np.array([0.0, 1.0])


def _embed(path: Path) -> Vector:
    return CAT if 'cat' in path.name.lower() else DOG


def _classify(path: Path, hint: str) -> Classification:
    """Fandom keyed off the filename, like the embed fake."""
    fandom = 'Cats' if 'cat' in path.name.lower() else 'Dogs'
    return Classification(
        fandom=fandom,
        filename=f'Fg{fandom}Calm',
        censored='censored' in path.name.lower(),
        confidence='high',
        description='a test image',
    )


DEPS = SortDeps(classify=_classify, embed=_embed)


def _jpeg(path: Path) -> Path:
    import hashlib

    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    rgb = tuple(hashlib.sha256(path.name.encode()).digest()[:3])
    Image.new('RGB', (8, 8), rgb).save(path, 'JPEG')  # unique bytes
    return path


def _seed_library(cfg: Settings) -> None:
    for i in range(3):
        _jpeg(cfg.pictures / 'Cats' / f'cat_{i}.jpg')
        _jpeg(cfg.pictures / 'Dogs' / f'dog_{i}.jpg')


def test_classify_pass_places_by_json_fandom(tmp_path: Path) -> None:
    """Pass 1: the JSON verdict names the file AND picks the folder."""
    from minion_core.adapters.files import has_week

    cfg = make_cfg(tmp_path / 'drive')
    _jpeg(cfg.inbox / 'new_cat.jpg')
    classify_pass(cfg, DEPS, '')
    placed = cfg.pictures / 'Cats' / 'FgCatsCalm.jpg'
    assert placed.exists()
    assert has_week(placed, cfg.week_tag)
    assert list(cfg.inbox.iterdir()) == []


def test_classify_pass_collision_gets_bare_digit(tmp_path: Path) -> None:
    """Library collisions stay valid prims: Name2, not Name_2."""
    cfg = make_cfg(tmp_path / 'drive')
    _jpeg(cfg.pictures / 'Cats' / 'FgCatsCalm.jpg')
    _jpeg(cfg.inbox / 'other_cat.jpg')
    classify_pass(cfg, DEPS, '')
    assert (cfg.pictures / 'Cats' / 'FgCatsCalm2.jpg').exists()


def test_classify_pass_rejects_non_images(tmp_path: Path) -> None:
    """Untrusted bytes are validated explicitly (BLUEPRINT 4)."""
    cfg = make_cfg(tmp_path / 'drive')
    fake = cfg.inbox / 'evil.jpg'
    fake.write_bytes(b'not an image at all')
    classify_pass(cfg, DEPS, '')
    assert fake.exists()  # left in place, never renamed


def test_classify_failure_leaves_source_for_retry(tmp_path: Path) -> None:
    """A frontier crash is logged; the file waits for the next run."""
    cfg = make_cfg(tmp_path / 'drive')
    pic = _jpeg(cfg.inbox / 'flaky_cat.jpg')

    def refuse(path: Path, hint: str) -> Classification:
        raise LlmError('over quota')

    classify_pass(cfg, SortDeps(classify=refuse, embed=_embed), '')
    assert pic.exists()


def test_censored_places_normally_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """censored=true is telemetry only: same placement, one log line."""
    import logging

    cfg = make_cfg(tmp_path / 'drive')
    _jpeg(cfg.inbox / 'censored_cat.jpg')
    with caplog.at_level(logging.INFO, logger='sort'):
        classify_pass(cfg, DEPS, '')
    assert (cfg.pictures / 'Cats' / 'FgCatsCalm.jpg').exists()
    assert any('censored=True' in r.getMessage() for r in caplog.records)


def test_classify_pass_forwards_the_hint(tmp_path: Path) -> None:
    """The weekly script hint reaches every classify call."""
    cfg = make_cfg(tmp_path / 'drive')
    _jpeg(cfg.inbox / 'scene_cat.jpg')
    seen: list[str] = []

    def spy(path: Path, hint: str) -> Classification:
        seen.append(hint)
        return _classify(path, hint)

    classify_pass(cfg, SortDeps(classify=spy, embed=_embed), 'week text')
    assert seen == ['week text']


def test_demote_pass_moves_sparse_to_unknown(tmp_path: Path) -> None:
    """Pass 2: fandoms under demote_min_count sink to Unknown."""
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

    deps = SortDeps(classify=_classify, embed=counting)
    run_passes(cfg, deps)
    assert not (cfg.pictures / 'Sparse').exists()  # demoted
    cats = [p.name for p in (cfg.pictures / 'Cats').iterdir()]
    assert 'lone_cat.jpg' in cats  # rescued by re-place
    library = [f'{k}_{i}.jpg' for k in ('cat', 'dog') for i in range(3)]
    for name in library:
        assert calls.count(name) == 1  # embedded once in its life


def test_replace_pass_rescues_unknown(tmp_path: Path) -> None:
    """Pass 3: Unknown re-matches against the new layout via CLIP."""
    cfg = make_cfg(tmp_path / 'drive')
    _seed_library(cfg)
    _jpeg(cfg.pictures / 'Unknown' / 'lost_cat.jpg')
    cache = EmbeddingCache(cfg)
    cache.invalidate()
    replace_pass(cfg, DEPS, cache)
    assert (cfg.pictures / 'Cats' / 'lost_cat.jpg').exists()


def test_full_run_end_to_end(tmp_path: Path) -> None:
    """The three passes compose: inbox image lands in its fandom."""
    cfg = make_cfg(tmp_path / 'drive')
    _seed_library(cfg)
    _jpeg(cfg.inbox / 'stray cat.jpg')
    run_passes(cfg, DEPS)
    cats = [p.name for p in (cfg.pictures / 'Cats').iterdir()]
    assert 'FgCatsCalm.jpg' in cats
    assert list(cfg.inbox.iterdir()) == []


def test_idle_run_exits_fast_and_writes_nothing(tmp_path: Path) -> None:
    """OPERATIONS 5: an idle run touches neither cache nor adapters."""
    cfg = make_cfg(tmp_path / 'drive')
    _seed_library(cfg)

    def explode(path: Path, hint: str) -> Classification:
        raise AssertionError('idle run must not call adapters')

    def explode_embed(path: Path) -> Vector:
        raise AssertionError('idle run must not call adapters')

    run_passes(cfg, SortDeps(classify=explode, embed=explode_embed))
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
    assert 'FgCatsCalm.jpg' in cats


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
    assert 'FgCatsCalm.jpg' in cats


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
