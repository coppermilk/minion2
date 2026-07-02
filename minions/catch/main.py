"""catch bot: immediate Downloads filing (streaming).

Graph: Folder(catch_dir) -> ClassifyCopy. There is deliberately no
disposal sink: Downloads is the user's folder, not the pipeline's;
the library copy is a copy and the original never leaves
(REQ-CATCH-001). Do not "fix" the missing DisposeSource.

Classification reuses the sort adapters (one placement logic, one
set of adapters -- BLUEPRINT 11 adds no new frontier here).
"""

from __future__ import annotations

import functools
import logging
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from minion_core.adapters import llm
from minion_core.adapters import vision
from minion_core.adapters.files import atomic_write
from minion_core.adapters.files import next_free_path
from minion_core.adapters.files import sanitize
from minion_core.adapters.files import stem
from minion_core.adapters.files import valid_image
from minion_core.adapters.vision import EmbeddingCache
from minion_core.adapters.vision import nearest_fandom
from minion_core.kernel import Disposition
from minion_core.kernel import Folder
from minion_core.kernel import FolderSpec
from minion_core.kernel import SeenPaths
from minion_core.kernel import Step
from minion_core.kernel import Verdict
from minion_core.kernel import bot_logger
from minion_core.kernel import run
from minion_core.settings import UNKNOWN
from minion_core.settings import load

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Mapping
    from pathlib import Path

    from minion_core.adapters.vision import Embedder
    from minion_core.kernel import Job
    from minion_core.kernel import Stage
    from minion_core.settings import Settings

BOT = 'catch'

_LOG = logging.getLogger(BOT)

_LABELLED = re.compile(r'^\d{4}_loc_')
"""A name already carrying the canonical stem: processed earlier."""


@dataclass(frozen=True)
class CatchDeps:
    """The non-deterministic frontier, injected (BLUEPRINT 11)."""

    namer: Callable[[Path], str]
    embed: Embedder


def real_deps(env: Mapping[str, str]) -> CatchDeps:
    """Wire the live adapters (tests inject doubles instead)."""
    spec = llm.spec_from(env)
    return CatchDeps(
        namer=functools.partial(llm.name_image, spec=spec),
        embed=vision.embed_image,
    )


class ClassifyCopy(Step):
    """The bot's one transformation: label, copy, rename in place.

    The copy lands in ``pictures/<Fandom>/`` collision-free and
    atomically (REQ-DATA-001/002 inherited); the original is renamed
    to the same labelled stem but never moved or deleted
    (REQ-CATCH-001).
    """

    def __init__(self, cfg: Settings, deps: CatchDeps) -> None:
        self._cfg = cfg
        self._deps = deps
        self._cache = EmbeddingCache(cfg)

    def process(self, job: Job) -> Verdict:
        """Classify one new download; failures leave it untouched."""
        if _LABELLED.match(job.src.name):
            return Verdict(Disposition.SKIPPED, reason='already_labelled')
        if not valid_image(job.src):
            return Verdict(Disposition.REJECTED, reason='bad_image')
        try:
            label, fandom = self._classify(job.src)
        except Exception:  # noqa: BLE001 -- REQ-CATCH-002: log + FAILED
            _LOG.exception('classify_failed src=%s', job.src)
            return Verdict(Disposition.FAILED, reason='classify_failed')
        return self._file_copy(job.src, label, fandom)

    def _classify(self, src: Path) -> tuple[str, str]:
        label = sanitize(self._deps.namer(src))
        library = self._cache.refresh(self._cfg.pictures, self._deps.embed)
        fandom = nearest_fandom(self._deps.embed(src), library) or UNKNOWN
        return label, fandom

    def _file_copy(self, src: Path, label: str, fandom: str) -> Verdict:
        named = stem(label, 'loc') + src.suffix.lower()
        target = next_free_path(self._cfg.pictures / fandom / named)
        atomic_write(target, src.read_bytes())
        renamed = next_free_path(src.with_name(named))
        src.rename(renamed)  # in place: Downloads stays browsable
        return Verdict(
            Disposition.DELIVERED,
            result=target,
            reply=f'filed {target.name} -> {fandom}',
        )


def build(cfg: Settings, deps: CatchDeps) -> Stage:
    """Assemble the belt: watch Downloads, classify, copy."""
    if cfg.catch_dir is None:
        raise ValueError('bad_config: CATCH_DIR is required for catch')
    spec = FolderSpec(
        root=cfg.catch_dir,
        dest=cfg.pictures,
        exts=vision.IMAGE_EXTS,
        poll_sec=cfg.poll_sec,
    )
    seen = SeenPaths(cfg.seen_paths_max)
    return Folder(spec, seen) >> ClassifyCopy(cfg, deps)


def main(env: Mapping[str, str] | None = None) -> int:
    """Build Settings once and drain the belt."""
    mapping = os.environ if env is None else env
    cfg = load(mapping)
    if cfg.catch_dir is None:
        log = bot_logger(BOT, cfg.logs)
        log.warning('skipped reason=bad_config catch_dir unset')
        return 0
    return run(BOT, build(cfg, real_deps(mapping)), cfg.logs)


if __name__ == '__main__':
    raise SystemExit(main())
