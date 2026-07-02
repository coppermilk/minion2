"""Vision boundary: embeddings, nearest fandom, person boxes, faces.

Heavy vendors (torch, torchvision, transformers, facenet) load
lazily inside the functions that use them, so a bot needing no ML --
and the test suite -- never loads them (Power-of-10 rule 3). numpy is
the cache format and loads eagerly.

The embedding cache is CACHE-class data (BLUEPRINT 1.2): disposable,
rebuilds unattended, wiped by Demote (REQ-SORT-001).
"""

from __future__ import annotations

import functools
import itertools
import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import TypeAlias

import numpy as np

from minion_core.adapters.files import HideSpec
from minion_core.adapters.files import hide_boxes
from minion_core.adapters.files import load_rgb
from minion_core.kernel import Disposition
from minion_core.kernel import Step
from minion_core.kernel import Verdict
from minion_core.settings import UNKNOWN

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterator

    from minion_core.kernel import Job
    from minion_core.settings import Settings

_LOG = logging.getLogger('vision')

Vector: TypeAlias = 'np.ndarray[Any, Any]'
Embedder: TypeAlias = 'Callable[[Path], Vector]'
Box: TypeAlias = tuple[int, int, int, int]

IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.webp')
"""Image suffixes the vision passes consider."""

PERSON_SCORE_MIN = 0.7
"""Detection confidence below which a person box is noise."""


class EmbeddingCache:
    """Incremental ``(path, fandom)``-keyed vectors in regen/.

    Recomputes only new files, drops removed ones, and caps the scan
    at ``max_embedding_scan`` (OPERATIONS 4). Keys are
    ``<fandom>|<file name>``.
    """

    def __init__(self, cfg: Settings) -> None:
        self._file = cfg.regen / '_embeddings.npz'
        self._cap = cfg.max_embedding_scan

    def invalidate(self) -> None:
        """Drop the cache (REQ-SORT-001: Demote calls this)."""
        self._file.unlink(missing_ok=True)

    def refresh(self, root: Path, embed: Embedder) -> dict[str, Vector]:
        """Sync vectors with the fandom tree under ``root``.

        An unchanged tree writes nothing: idle cron runs and
        Drive-synced deployments cost zero rewrites.
        """
        old = self._load()
        new: dict[str, Vector] = {}
        computed = False
        for fandom, path in _tree(root, self._cap):
            key = f'{fandom}|{path.name}'
            known = old.get(key)
            if known is None:
                known = embed(path)
                computed = True
            new[key] = known
        if computed or new.keys() != old.keys():
            self._save(new)
        return new

    def _load(self) -> dict[str, Vector]:
        if not self._file.is_file():
            return {}
        try:
            with np.load(self._file) as data:
                return {key: data[key] for key in data.files}
        except (OSError, ValueError):
            _LOG.warning('cache unreadable; rebuilding: %s', self._file)
            return {}

    def _save(self, vectors: dict[str, Vector]) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        fd, raw = tempfile.mkstemp(dir=self._file.parent, suffix='.part')
        tmp = Path(raw)
        try:
            with os.fdopen(fd, 'wb') as fh:
                # Waiver: numpy stubs type **kwds via allow_pickle;
                # keyword arrays are the documented savez call form.
                np.savez(fh, **vectors)  # type: ignore[arg-type]
            tmp.replace(self._file)
        finally:
            tmp.unlink(missing_ok=True)


def _tree(root: Path, cap: int) -> Iterator[tuple[str, Path]]:
    """Yield ``(fandom, image)`` pairs, scan capped (bounded)."""
    pairs = (
        (d.name, p)
        for d in _fandom_dirs(root)
        for p in sorted(d.iterdir())
        if p.suffix.lower() in IMAGE_EXTS
    )
    yield from itertools.islice(pairs, cap)


def _fandom_dirs(root: Path) -> list[Path]:
    """Every fandom directory except Unknown."""
    return sorted(
        p for p in root.iterdir() if p.is_dir() and p.name != UNKNOWN
    )


def nearest_fandom(vec: Vector, library: dict[str, Vector]) -> str | None:
    """The fandom of the most similar library vector (cosine)."""
    best_key: str | None = None
    best_sim = -2.0
    for key, other in library.items():
        sim = _cosine(vec, other)
        if sim > best_sim:
            best_sim = sim
            best_key = key
    return best_key.split('|', 1)[0] if best_key else None


def _cosine(a: Vector, b: Vector) -> float:
    norm = float(np.linalg.norm(a) * np.linalg.norm(b))
    if norm == 0.0:
        return -1.0
    return float(np.dot(a, b) / norm)


@functools.lru_cache(maxsize=1)
def _clip() -> tuple[Any, Any]:
    """Load CLIP once; weights persist in CACHE via TORCH_HOME."""
    from transformers import CLIPModel
    from transformers import CLIPProcessor

    name = 'openai/clip-vit-base-patch32'
    return (
        CLIPModel.from_pretrained(name),
        CLIPProcessor.from_pretrained(name),
    )


def embed_image(path: Path) -> Vector:
    """CLIP image embedding (lazy torch + transformers load)."""
    import torch

    model, processor = _clip()
    batch = processor(images=load_rgb(path), return_tensors='pt')
    with torch.no_grad():
        feats = model.get_image_features(**batch)
    vec: Vector = feats[0].numpy()
    return vec


def person_boxes(path: Path) -> tuple[Box, ...]:
    """Bounding boxes of detected people (lazy torchvision load)."""
    import torch
    from torchvision.models import detection
    from torchvision.transforms import functional as tvf

    model = _detector(detection)
    tensor = tvf.to_tensor(load_rgb(path))
    with torch.no_grad():
        found = model([tensor])[0]
    return _to_boxes(found)


@functools.lru_cache(maxsize=1)
def _detector(detection: Any) -> Any:  # noqa: ANN401 -- vendor module handle
    model = detection.fasterrcnn_resnet50_fpn(weights='DEFAULT')
    model.eval()
    return model


def _to_boxes(found: dict[str, Any]) -> tuple[Box, ...]:
    """Keep confident person detections only."""
    boxes: list[Box] = []
    for label, score, box in zip(
        found['labels'], found['scores'], found['boxes'], strict=True
    ):
        if int(label) != 1 or float(score) < PERSON_SCORE_MIN:
            continue
        x0, y0, x1, y1 = (int(v) for v in box)
        boxes.append((x0, y0, x1, y1))
    return tuple(boxes)


def face_boxes(path: Path) -> tuple[Box, ...]:
    """Bounding boxes of detected faces (lazy facenet load)."""
    found, _ = _mtcnn().detect(load_rgb(path))
    if found is None:
        return ()
    return tuple((int(a), int(b), int(c), int(d)) for a, b, c, d in found)


@functools.lru_cache(maxsize=1)
def _mtcnn() -> Any:  # noqa: ANN401 -- vendor model handle
    from facenet_pytorch import MTCNN

    return MTCNN(keep_all=True)


class HidePeople(Step):
    """Hide every detected person (the censor family's one step).

    Correctness is CT-B: a missed person leaks the hidden subject,
    so detections apply verbatim and zero detections is a SKIP,
    never a silent pass-through of the original. The mode is fixed
    per bot (files.BLUR / files.BLACK), not a runtime knob.
    """

    def __init__(self, mode: str) -> None:
        self._mode = mode

    def process(self, job: Job) -> Verdict:
        """Detect and hide; the ``_s1`` copy is the result."""
        boxes = person_boxes(job.src)
        if not boxes:
            return Verdict(
                Disposition.SKIPPED,
                reason='no_person',
                reply='no people found',
            )
        out = job.dest / f'{job.src.stem}_s1{job.src.suffix}'
        hide_boxes(job.src, out, HideSpec(boxes=boxes, mode=self._mode))
        return Verdict(Disposition.DELIVERED, result=out, reply='censored')
