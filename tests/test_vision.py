"""vision cache tests: REQ-SORT-001 and the incremental contract."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from minion_core.adapters.vision import EmbeddingCache
from minion_core.adapters.vision import nearest_fandom
from tests.conftest import make_cfg

if TYPE_CHECKING:
    from pathlib import Path

    from minion_core.adapters.vision import Vector
    from minion_core.settings import Settings


def _vec(seed: int) -> Vector:
    rng = np.random.default_rng(seed)
    return rng.random(8)


class CountingEmbedder:
    """Embedder double: deterministic vectors, counted calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, path: Path) -> Vector:
        self.calls.append(path.name)
        return _vec(abs(hash(path.name)) % 1000)


def _library(cfg: Settings, layout: dict[str, list[str]]) -> None:
    for fandom, names in layout.items():
        (cfg.pictures / fandom).mkdir(parents=True, exist_ok=True)
        for name in names:
            (cfg.pictures / fandom / name).write_bytes(b'img')


def test_refresh_is_incremental(tmp_path: Path) -> None:
    """Only new files are embedded; removed keys drop out."""
    cfg = make_cfg(tmp_path / 'drive')
    _library(cfg, {'A': ['1.jpg', '2.jpg']})
    cache = EmbeddingCache(cfg)
    embed = CountingEmbedder()
    first = cache.refresh(cfg.pictures, embed)
    assert sorted(first) == ['A|1.jpg', 'A|2.jpg']
    assert len(embed.calls) == 2

    (cfg.pictures / 'A' / '2.jpg').unlink()
    (cfg.pictures / 'A' / '3.jpg').write_bytes(b'img')
    second = cache.refresh(cfg.pictures, embed)
    assert sorted(second) == ['A|1.jpg', 'A|3.jpg']
    assert embed.calls.count('1.jpg') == 1  # cache hit, not re-run
    assert embed.calls.count('3.jpg') == 1


def test_scan_is_capped(tmp_path: Path) -> None:
    """max_embedding_scan bounds the walk (BLUEPRINT 10)."""
    cfg = make_cfg(tmp_path / 'drive', MAX_EMBEDDING_SCAN='3')
    _library(cfg, {'A': [f'{i}.jpg' for i in range(10)]})
    got = EmbeddingCache(cfg).refresh(cfg.pictures, CountingEmbedder())
    assert len(got) == 3


def test_invalidate_forces_full_rebuild(tmp_path: Path) -> None:
    """REQ-SORT-001: after Demote the cache must not serve ghosts."""
    cfg = make_cfg(tmp_path / 'drive')
    _library(cfg, {'A': ['1.jpg'], 'Sparse': ['s.jpg']})
    cache = EmbeddingCache(cfg)
    embed = CountingEmbedder()
    cache.refresh(cfg.pictures, embed)
    assert 'Sparse|s.jpg' in cache.refresh(cfg.pictures, embed)

    # Demote moves the sparse fandom away and invalidates.
    (cfg.pictures / 'Sparse' / 's.jpg').rename(
        cfg.pictures / 'A' / 's.jpg',
    )
    (cfg.pictures / 'Sparse').rmdir()
    cache.invalidate()
    rebuilt = cache.refresh(cfg.pictures, embed)
    assert sorted(rebuilt) == ['A|1.jpg', 'A|s.jpg']
    assert embed.calls.count('s.jpg') == 2  # stale vector recomputed


def test_unknown_is_not_a_reference_fandom(tmp_path: Path) -> None:
    """Unknown never teaches the matcher (Re-place semantics)."""
    cfg = make_cfg(tmp_path / 'drive')
    _library(cfg, {'A': ['1.jpg'], 'Unknown': ['u.jpg']})
    got = EmbeddingCache(cfg).refresh(cfg.pictures, CountingEmbedder())
    assert list(got) == ['A|1.jpg']


def test_corrupt_cache_rebuilds_unattended(tmp_path: Path) -> None:
    """CACHE is disposable: corruption is a rebuild, not a crash."""
    cfg = make_cfg(tmp_path / 'drive')
    _library(cfg, {'A': ['1.jpg']})
    (cfg.regen / '_embeddings.npz').write_bytes(b'not an npz')
    got = EmbeddingCache(cfg).refresh(cfg.pictures, CountingEmbedder())
    assert list(got) == ['A|1.jpg']


def test_unchanged_tree_writes_nothing(tmp_path: Path) -> None:
    """Idle economy: an unchanged tree never rewrites the npz."""
    cfg = make_cfg(tmp_path / 'drive')
    _library(cfg, {'A': ['1.jpg']})
    cache = EmbeddingCache(cfg)
    embed = CountingEmbedder()
    cache.refresh(cfg.pictures, embed)
    npz = cfg.regen / '_embeddings.npz'
    before = npz.stat()

    again = cache.refresh(cfg.pictures, embed)
    after = npz.stat()
    assert list(again) == ['A|1.jpg']
    assert embed.calls.count('1.jpg') == 1  # no recompute
    assert (after.st_mtime_ns, after.st_ino) == (
        before.st_mtime_ns,
        before.st_ino,
    )


def test_two_processes_share_one_cache(tmp_path: Path) -> None:
    """Containers on one mount interleave safely (OPERATIONS 4).

    Two independent EmbeddingCache instances stand in for sort and
    catch running in different containers against the same
    ``regen/_embeddings.npz``: last-writer-wins is at worst a
    recompute, and every refresh serves exactly the live tree.
    """
    cfg = make_cfg(tmp_path / 'drive')
    _library(cfg, {'A': ['1.jpg']})
    sort_side = EmbeddingCache(cfg)
    catch_side = EmbeddingCache(cfg)
    embed = CountingEmbedder()

    first = sort_side.refresh(cfg.pictures, embed)
    assert list(first) == ['A|1.jpg']

    # catch files a new image while sort is between runs
    (cfg.pictures / 'B').mkdir()
    (cfg.pictures / 'B' / '2.jpg').write_bytes(b'img')
    second = catch_side.refresh(cfg.pictures, embed)
    assert sorted(second) == ['A|1.jpg', 'B|2.jpg']
    assert embed.calls.count('1.jpg') == 1  # reused, not recomputed

    # sort demotes B mid-flight and invalidates; catch refreshes
    # concurrently -- the rebuilt cache still mirrors the live tree
    (cfg.pictures / 'B' / '2.jpg').unlink()
    (cfg.pictures / 'B').rmdir()
    sort_side.invalidate()
    third = catch_side.refresh(cfg.pictures, embed)
    assert list(third) == ['A|1.jpg']  # no ghosts, no crash


def test_nearest_fandom_by_cosine() -> None:
    """The closest vector's fandom wins; empty library is None."""
    target = np.array([1.0, 0.0])
    library = {
        'Cats|a.jpg': np.array([0.9, 0.1]),
        'Dogs|b.jpg': np.array([0.0, 1.0]),
    }
    assert nearest_fandom(target, library) == 'Cats'
    assert nearest_fandom(target, {}) is None
