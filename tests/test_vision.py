"""vision cache tests: REQ-SORT-001 and the incremental contract."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from minion_core.adapters.vision import EmbeddingCache
from minion_core.adapters.vision import nearest_fandom
from minion_core.adapters.vision import nearest_named
from tests.conftest import make_cfg

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from minion_core.adapters.vision import Vector
    from minion_core.settings import Settings


def _vec(seed: int) -> Vector:
    rng = np.random.default_rng(seed)
    return rng.random(8)


def test_nearest_named_returns_key_and_score() -> None:
    """The props path needs the full key plus the cosine to threshold."""
    library = {
        'F|PrWand': np.array([1.0, 0.0], dtype=np.float32),
        'F|PrBag': np.array([0.0, 1.0], dtype=np.float32),
    }
    key, sim = nearest_named(np.array([0.9, 0.1], dtype=np.float32), library)
    assert key == 'F|PrWand'
    assert sim > 0.9


def test_pool1d_flattens_multidim_keeps_1d() -> None:
    """A stray (tokens, dim) embedding pools to 1-D; a 1-D one is kept."""
    from minion_core.adapters.vision import _pool1d

    flat = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    assert _pool1d(flat).shape == (3,)
    tokens = np.ones((50, 768), dtype=np.float32)
    assert _pool1d(tokens).shape == (768,)


def test_nearest_named_empty_library() -> None:
    """An empty library yields no key and a sentinel score."""
    key, sim = nearest_named(np.array([1.0, 0.0], dtype=np.float32), {})
    assert key == ''
    assert sim == -2.0


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
            (cfg.pictures / fandom / name).write_bytes(name.encode())


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
    (cfg.pictures / 'A' / '3.jpg').write_bytes(b'3.jpg')
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


def test_move_between_fandoms_never_recomputes(tmp_path: Path) -> None:
    """REQ-SORT-001: identity keys survive a demote-style move."""
    cfg = make_cfg(tmp_path / 'drive')
    _library(cfg, {'A': ['1.jpg'], 'Sparse': ['s.jpg']})
    cache = EmbeddingCache(cfg)
    embed = CountingEmbedder()
    assert 'Sparse|s.jpg' in cache.refresh(cfg.pictures, embed)

    # A demote-style move: the file changes fandom, not identity.
    (cfg.pictures / 'Sparse' / 's.jpg').rename(
        cfg.pictures / 'A' / 's.jpg',
    )
    (cfg.pictures / 'Sparse').rmdir()
    remapped = cache.refresh(cfg.pictures, embed)
    assert sorted(remapped) == ['A|1.jpg', 'A|s.jpg']  # live layout
    assert embed.calls.count('s.jpg') == 1  # vector reused, not ghosted


def test_invalidate_forces_full_rebuild(tmp_path: Path) -> None:
    """The manual recovery tool really does wipe and recompute."""
    cfg = make_cfg(tmp_path / 'drive')
    _library(cfg, {'A': ['1.jpg']})
    cache = EmbeddingCache(cfg)
    embed = CountingEmbedder()
    cache.refresh(cfg.pictures, embed)
    cache.invalidate()
    assert list(cache.refresh(cfg.pictures, embed)) == ['A|1.jpg']
    assert embed.calls.count('1.jpg') == 2  # recomputed after wipe


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
    (cfg.pictures / 'B' / '2.jpg').write_bytes(b'B-2.jpg')
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


def test_live_duplicates_share_one_verified_vector(
    tmp_path: Path,
) -> None:
    """Byte-identical files share a vector by byte comparison."""
    cfg = make_cfg(tmp_path / 'drive')
    (cfg.pictures / 'A').mkdir()
    (cfg.pictures / 'B').mkdir()
    (cfg.pictures / 'A' / 'one.jpg').write_bytes(b'same-bytes')
    (cfg.pictures / 'B' / 'two.jpg').write_bytes(b'same-bytes')
    embed = CountingEmbedder()
    got = EmbeddingCache(cfg).refresh(cfg.pictures, embed)
    assert sorted(got) == ['A|one.jpg', 'B|two.jpg']
    assert len(embed.calls) == 1  # embedded once, verified, shared
    assert np.array_equal(got['A|one.jpg'], got['B|two.jpg'])


def test_hash_collision_is_detected_and_split(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The 100% guarantee for coexisting files: bytes decide.

    Even if the digest lies (forced equal here), differing bytes are
    detected by direct comparison and the files get distinct keys --
    a wrong vector is never served.
    """
    cfg = make_cfg(tmp_path / 'drive')
    (cfg.pictures / 'A').mkdir()
    (cfg.pictures / 'B').mkdir()
    (cfg.pictures / 'A' / 'one.jpg').write_bytes(b'AAAA')
    (cfg.pictures / 'B' / 'two.jpg').write_bytes(b'BBBB')

    class _LyingHash:
        def __init__(self, data: bytes) -> None:
            pass

        def hexdigest(self) -> str:
            return 'deadbeef'

    from minion_core.adapters import vision

    monkeypatch.setattr(vision.hashlib, 'sha256', _LyingHash)
    embed = CountingEmbedder()
    with caplog.at_level('CRITICAL', logger='vision'):
        got = EmbeddingCache(cfg).refresh(cfg.pictures, embed)
    assert 'hash_collision' in caplog.text
    assert len(embed.calls) == 2  # split: each content embedded alone
    assert not np.array_equal(got['A|one.jpg'], got['B|two.jpg'])


def test_nearest_fandom_by_cosine() -> None:
    """The closest vector's fandom wins; empty library is None."""
    target = np.array([1.0, 0.0])
    library = {
        'Cats|a.jpg': np.array([0.9, 0.1]),
        'Dogs|b.jpg': np.array([0.0, 1.0]),
    }
    assert nearest_fandom(target, library) == 'Cats'
    assert nearest_fandom(target, {}) is None


def test_nearest_fandom_below_tau_is_none() -> None:
    """A weak best match is rejected, not forced into the nearest fandom."""
    target = np.array([1.0, 0.0])
    library = {'Cats|a.jpg': np.array([0.6, 0.8])}  # cosine ~0.6
    assert nearest_fandom(target, library, tau=0.9) is None  # too unlike
    assert nearest_fandom(target, library, tau=0.5) == 'Cats'  # close enough
