"""The three named passes of sort (BLUEPRINT 9).

Classify -> Demote -> Re-place. The Gemini JSON verdict decides
both the prim name and the fandom, but the image STAYS in its
source dir for the working week: the name becomes the prim, the
fandom goes into EXIF (files.tag_fandom), and the Monday week-clean
run moves the classified week into ``pictures/`` (BLUEPRINT 9).
CLIP embeddings survive only to rescue images out of Unknown/.
Batch bots are pure functions of the folder state at start; every
scan is capped (bounded loops).
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from minion_core.adapters import scripts
from minion_core.adapters.files import PRIM_NAMED
from minion_core.adapters.files import move_atomic
from minion_core.adapters.files import next_free_prim
from minion_core.adapters.files import tag_fandom
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
    from minion_core.adapters.vision import Vector
    from minion_core.settings import Settings

_LOG = logging.getLogger('sort')


@dataclass(frozen=True)
class SortDeps:
    """The non-deterministic frontier, injected (BLUEPRINT 11)."""

    classify: Callable[[Path, str], Classification]
    embed: Embedder


def run_passes(cfg: Settings, deps: SortDeps) -> None:
    """Classify -> Demote -> Re-place, in that order.

    An idle run (nothing to classify, nothing to re-place) exits
    before touching the cache or any adapter (OPERATIONS 5): the
    tight cron cadence costs nothing while asleep. The weekly
    script hint is read once per run, so a fresh ``.gdoc`` drop is
    consumed by the very run its images trigger.
    """
    if _idle(cfg):
        _LOG.info('idle: nothing to sort')
        return
    _best_effort(
        'classify', lambda: classify_pass(cfg, deps, scripts.script_hint(cfg))
    )
    _best_effort('demote', lambda: demote_pass(cfg))
    _best_effort(
        'replace', lambda: replace_pass(cfg, deps, EmbeddingCache(cfg))
    )


def _best_effort(name: str, run: Callable[[], None]) -> None:
    """Run one pass; contain and log any crash with its traceback.

    A failure in one pass (a bad model, a corrupt image, a torch load)
    must not sink the whole trigger as an opaque ``step_crashed``: the
    other passes still run, classify's per-image work has already
    landed, and the real cause is written to ``sort.log`` under a
    ``<pass>_failed`` code instead of vanishing to stdout only.
    """
    try:
        run()
    except Exception:
        logging.getLogger('sort').exception('%s_failed', name)


def _idle(cfg: Settings) -> bool:
    """Whether this run has no work at all.

    Already-classified images waiting out their week in the source
    dirs are not work; only unclassified arrivals and Unknown/ are.
    """
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
    """Every UNclassified image waiting in the source dirs.

    A prim-shaped name marks a file this pass already handled; it
    rests in place until the Monday mover (week-clean) collects it,
    and must not re-trigger the model.
    """
    dirs = cfg.source_dirs or (cfg.inbox,)
    cap = cfg.max_embedding_scan
    return [
        p
        for d in dirs
        for p in _images(d, cap)
        if not PRIM_NAMED.match(p.name)
    ]


def classify_pass(cfg: Settings, deps: SortDeps, hint: str) -> None:
    """Pass 1: one JSON verdict; the image keeps waiting in place.

    The name becomes the prim, the fandom goes into EXIF, the weekly
    tag marks the working set -- the file itself stays in its source
    dir until the Monday mover. Everything is decided HERE, during
    the week: when the model punts (Unknown), CLIP picks the nearest
    library fandom immediately, so Monday is purely mechanical. A
    failed classification leaves the file untouched -- logged,
    retried on the next run (the model is a frontier, not a gate).
    ``censored`` is telemetry only (operator decision).
    """
    decide = _clip_fallback(cfg, deps)
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
        target = path.rename(next_free_prim(path.with_name(named)))
        fandom, via = decide(verdict.fandom, target)
        tag_fandom(target, fandom)
        tag_week(target, cfg.week_tag)
        _LOG.info(
            'classified src=%s fandom=%s via=%s confidence=%s censored=%s',
            target.name,
            fandom,
            via,
            verdict.confidence,
            verdict.censored,
        )


def _clip_fallback(
    cfg: Settings, deps: SortDeps
) -> Callable[[str, Path], tuple[str, str]]:
    """A lazy nearest-fandom decider for verdicts the model punted.

    CLIP (and the embedding cache) load only if some verdict is
    Unknown; the library is refreshed at most once per pass.
    """
    library: dict[str, Vector] | None = None

    def decide(fandom: str, path: Path) -> tuple[str, str]:
        nonlocal library
        if fandom != UNKNOWN:
            return fandom, 'gemini'
        if library is None:
            library = EmbeddingCache(cfg).refresh(cfg.pictures, deps.embed)
        match = nearest_fandom(deps.embed(path), library, cfg.sort_tau)
        return match or UNKNOWN, 'clip'

    return decide


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

    Library hygiene, not week logic: whatever demote (or a lost EXIF
    tag) parked in Unknown/ re-matches the nearest labelled fandom.
    """
    library = cache.refresh(cfg.pictures, deps.embed)
    if not library:
        return
    for path in _images(cfg.pictures / UNKNOWN, cfg.max_embedding_scan):
        fandom = nearest_fandom(deps.embed(path), library, cfg.sort_tau)
        if fandom is None:
            continue
        target = move_atomic(
            path, next_free_prim(cfg.pictures / fandom / path.name)
        )
        _LOG.info('placed src=%s fandom=%s', target.name, fandom)
