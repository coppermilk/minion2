"""The four named passes of sort (BLUEPRINT 9).

Name -> Place -> Demote -> Re-place. Reply parsing and placement
live here, with the bot (BLUEPRINT 11): one LLM reply maps to
exactly one placement. Batch bots are pure functions of the folder
state at start; every scan is capped (bounded loops).
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from minion_core.adapters.files import move_atomic
from minion_core.adapters.files import next_free_path
from minion_core.adapters.files import sanitize
from minion_core.adapters.files import stem
from minion_core.adapters.files import tag_week
from minion_core.adapters.files import valid_image
from minion_core.adapters.vision import IMAGE_EXTS
from minion_core.adapters.vision import EmbeddingCache
from minion_core.adapters.vision import nearest_fandom
from minion_core.settings import UNKNOWN

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from minion_core.adapters.vision import Embedder
    from minion_core.settings import Settings

_LOG = logging.getLogger('sort')


@dataclass(frozen=True)
class SortDeps:
    """The non-deterministic frontier, injected (BLUEPRINT 11)."""

    namer: Callable[[Path], str]
    embed: Embedder


def run_passes(cfg: Settings, deps: SortDeps) -> None:
    """Name -> Place -> Demote -> Re-place, in that order.

    An idle run (nothing to place, nothing to re-place) exits
    before touching the cache or any adapter (OPERATIONS 5): the
    tight cron cadence costs nothing while asleep.
    """
    if _idle(cfg):
        _LOG.info('idle: nothing to sort')
        return
    cache = EmbeddingCache(cfg)
    name_pass(cfg, deps)
    place_pass(cfg, deps, cache)
    demote_pass(cfg, cache)  # invalidates the cache (REQ-SORT-001)
    replace_pass(cfg, deps, cache)


def _idle(cfg: Settings) -> bool:
    """Whether this run has no work at all."""
    if _source_images(cfg):
        return False
    return not _images(cfg.pictures / UNKNOWN, cfg.max_embedding_scan)


def _images(root: Path, cap: int) -> list[Path]:
    """Images directly under ``root``, scan capped (bounded)."""
    if not root.is_dir():
        return []
    found = (
        p for p in sorted(root.iterdir()) if p.suffix.lower() in IMAGE_EXTS
    )
    return list(itertools.islice(found, cap))


def _source_images(cfg: Settings) -> list[Path]:
    """Every image waiting in the configured source dirs."""
    dirs = cfg.source_dirs or (cfg.inbox,)
    cap = cfg.max_embedding_scan
    return [p for d in dirs for p in _images(d, cap)]


def name_pass(cfg: Settings, deps: SortDeps) -> None:
    """Pass 1: the LLM labels each image; the label is the name."""
    for path in _source_images(cfg):
        if not valid_image(path):
            _LOG.warning('rejected reason=bad_image src=%s', path)
            continue
        label = sanitize(deps.namer(path))
        named = stem(label, 'loc') + path.suffix.lower()
        move_atomic(path, next_free_path(path.with_name(named)))


def place_pass(cfg: Settings, deps: SortDeps, cache: EmbeddingCache) -> None:
    """Pass 2: nearest fandom decides the folder."""
    library = cache.refresh(cfg.pictures, deps.embed)
    for path in _source_images(cfg):
        fandom = nearest_fandom(deps.embed(path), library) or UNKNOWN
        _place(path, cfg.pictures / fandom, cfg.week_tag)


def _place(path: Path, into: Path, week_tag: str) -> None:
    """Move one image into its fandom, carrying the weekly tag."""
    target = move_atomic(path, next_free_path(into / path.name))
    tag_week(target, week_tag)
    _LOG.info('placed src=%s fandom=%s', target.name, into.name)


def demote_pass(cfg: Settings, cache: EmbeddingCache) -> None:
    """Pass 3: sparse fandoms sink to Unknown; the cache dies.

    Skipping the invalidation is a silent-misplacement defect
    (REQ-SORT-001), not an optimization.
    """
    for fandom_dir in _fandoms(cfg.pictures):
        members = _images(fandom_dir, cfg.max_embedding_scan)
        if not members or len(members) >= cfg.demote_min_count:
            continue
        for path in members:
            unknown = cfg.pictures / UNKNOWN / path.name
            move_atomic(path, next_free_path(unknown))
        fandom_dir.rmdir()
        _LOG.info('demoted fandom=%s count=%d', fandom_dir.name, len(members))
    cache.invalidate()  # REQ-SORT-001: before Re-place


def _fandoms(pictures: Path) -> list[Path]:
    """Every fandom directory except Unknown."""
    if not pictures.is_dir():
        return []
    return [
        p
        for p in sorted(pictures.iterdir())
        if p.is_dir() and p.name != UNKNOWN
    ]


def replace_pass(cfg: Settings, deps: SortDeps, cache: EmbeddingCache) -> None:
    """Pass 4: re-run placement against the new layout."""
    library = cache.refresh(cfg.pictures, deps.embed)
    if not library:
        return
    for path in _images(cfg.pictures / UNKNOWN, cfg.max_embedding_scan):
        fandom = nearest_fandom(deps.embed(path), library)
        if fandom is None:
            continue
        _place(path, cfg.pictures / fandom, cfg.week_tag)
