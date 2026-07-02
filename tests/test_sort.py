"""sort bot tests: the four passes and REQ-SORT-001 ordering."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pytest

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
    demote_pass(cfg, EmbeddingCache(cfg))
    assert not (cfg.pictures / 'Sparse').exists()
    assert (cfg.pictures / 'Unknown' / 'one_cat.jpg').exists()
    assert (cfg.pictures / 'Cats').exists()  # big fandoms stay


def test_demote_invalidates_cache_before_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REQ-SORT-001: the order is demote -> invalidate -> refresh."""
    cfg = make_cfg(tmp_path / 'drive')
    _seed_library(cfg)
    calls: list[str] = []
    monkeypatch.setattr(
        EmbeddingCache,
        'invalidate',
        lambda self: calls.append('invalidate'),
    )
    real_refresh = EmbeddingCache.refresh

    def spy_refresh(
        self: EmbeddingCache, root: Path, embed: object
    ) -> dict[str, Vector]:
        calls.append('refresh')
        return real_refresh(self, root, embed)  # type: ignore[arg-type]

    monkeypatch.setattr(EmbeddingCache, 'refresh', spy_refresh)
    run_passes(cfg, DEPS)
    assert calls == ['refresh', 'invalidate', 'refresh']


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
