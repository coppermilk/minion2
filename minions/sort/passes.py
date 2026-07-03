"""The three named passes of sort (BLUEPRINT 9).

Classify-place -> Demote -> Re-place. Placement lives here, with
the bot (BLUEPRINT 11): one model verdict maps to exactly one
placement. The Gemini JSON verdict decides both the prim name and
the fandom folder; CLIP embeddings survive only to rescue images
out of Unknown/. Batch bots are pure functions of the folder state
at start; every scan is capped (bounded loops).
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from minion_core.adapters import scripts
from minion_core.adapters.files import move_atomic
from minion_core.adapters.files import next_free_prim
from minion_core.adapters.files import tag_week
from minion_core.adapters.files import valid_image
from minion_core.adapters.llm import LlmError
from minion_core.adapters.vision import IMAGE_EXTS
from minion_core.adapters.vision import EmbeddingCache
from minion_core.adapters.vision import nearest_fandom
from minion_core.settings import UNKNOWN

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from minion_core.adapters.llm import Classification
    from minion_core.adapters.vision import Embedder
    from minion_core.settings import Settings

_LOG = logging.getLogger('sort')


@dataclass(frozen=True)
class SortDeps:
    """The non-deterministic frontier, injected (BLUEPRINT 11)."""

    classify: Callable[[Path, str], Classification]
    embed: Embedder


def run_passes(cfg: Settings, deps: SortDeps) -> None:
    """Classify-place -> Demote -> Re-place, in that order.

    An idle run (nothing to place, nothing to re-place) exits
    before touching the cache or any adapter (OPERATIONS 5): the
    tight cron cadence costs nothing while asleep. The weekly
    script hint is read once per run, so a fresh ``.gdoc`` drop is
    consumed by the very run its images trigger.
    """
    if _idle(cfg):
        _LOG.info('idle: nothing to sort')
        return
    classify_pass(cfg, deps, scripts.script_hint(cfg))
    demote_pass(cfg)  # vectors survive: identity-keyed cache
    replace_pass(cfg, deps, EmbeddingCache(cfg))


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


def classify_pass(cfg: Settings, deps: SortDeps, hint: str) -> None:
    """Pass 1: one JSON verdict names the image AND picks the folder.

    A failed classification leaves the file in the source dir --
    logged, retried on the next run (the model is a frontier, not a
    gate). ``censored`` is telemetry only: the image is placed like
    any other (operator decision).
    """
    for path in _source_images(cfg):
        if not valid_image(path):
            _LOG.warning('rejected reason=bad_image src=%s', path)
            continue
        try:
            verdict = deps.classify(path, hint)
        except (LlmError, OSError):
            _LOG.exception('classify_failed src=%s', path)
            continue
        named = verdict.filename + path.suffix.lower()
        into = cfg.pictures / verdict.fandom
        target = move_atomic(path, next_free_prim(into / named))
        tag_week(target, cfg.week_tag)
        _LOG.info(
            'placed src=%s fandom=%s confidence=%s censored=%s',
            target.name,
            verdict.fandom,
            verdict.confidence,
            verdict.censored,
        )


def demote_pass(cfg: Settings) -> None:
    """Pass 2: sparse fandoms sink to Unknown; vectors survive.

    No invalidation is needed (REQ-SORT-001 restated): the cache is
    identity-keyed and the fandom mapping is rebuilt from the live
    tree on every refresh, so Re-place matches the new layout by
    construction -- at zero recomputation cost.
    """
    for fandom_dir in _fandoms(cfg.pictures):
        members = _images(fandom_dir, cfg.max_embedding_scan)
        if not members or len(members) >= cfg.demote_min_count:
            continue
        for path in members:
            unknown = cfg.pictures / UNKNOWN / path.name
            move_atomic(path, next_free_prim(unknown))
        fandom_dir.rmdir()
        _LOG.info('demoted fandom=%s count=%d', fandom_dir.name, len(members))


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
    """Pass 3: CLIP re-matches Unknown against the live layout.

    The one surviving embedding consumer (BLUEPRINT 9): Gemini never
    sees these images again, so the nearest labelled fandom decides.
    """
    library = cache.refresh(cfg.pictures, deps.embed)
    if not library:
        return
    for path in _images(cfg.pictures / UNKNOWN, cfg.max_embedding_scan):
        fandom = nearest_fandom(deps.embed(path), library)
        if fandom is None:
            continue
        target = move_atomic(
            path, next_free_prim(cfg.pictures / fandom / path.name)
        )
        tag_week(target, cfg.week_tag)
        _LOG.info('placed src=%s fandom=%s', target.name, fandom)
