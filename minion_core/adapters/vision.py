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
import hashlib
import itertools
import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import TypeAlias

import numpy as np

from minion_core.adapters.files import BLACK
from minion_core.adapters.files import BLUR
from minion_core.adapters.files import HideSpec
from minion_core.adapters.files import Mask
from minion_core.adapters.files import blur_masked
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

MASK_THRESHOLD = 0.5
"""Per-pixel probability above which a mask covers the person."""


class EmbeddingCache:
    """Append-only vectors in regen/, keyed by verified content.

    The stored key is ``SHA-256:byte-length``. Identity is never
    taken on faith among coexisting files: when two live files share
    a key, their bytes are compared directly -- equal bytes share
    one vector (a verified duplicate), unequal bytes are a detected
    collision, split onto distinct keys with a CRITICAL log. That
    makes wrong-vector service impossible for anything that can be
    checked; the sole unwitnessable case (a file deleted, a
    different one with the same digest AND length appearing later)
    sits at ~2**-256 and cannot be verified by any bounded key
    without storing full content copies.

    An image is embedded exactly once in its life; moves and renames
    (Demote, Re-place) reuse the vector. The fandom mapping is
    rebuilt from the live tree on every refresh and never persisted
    (REQ-SORT-001). The scan is capped at ``max_embedding_scan``.
    """

    def __init__(self, cfg: Settings) -> None:
        self._file = cfg.regen / '_embeddings.npz'
        self._cap = cfg.max_embedding_scan

    def invalidate(self) -> None:
        """Drop the cache -- a manual recovery tool (CACHE class)."""
        self._file.unlink(missing_ok=True)

    def refresh(self, root: Path, embed: Embedder) -> dict[str, Vector]:
        """Return the live ``fandom|name -> vector`` library.

        Embeds only identities never seen before; an unchanged tree
        writes nothing, so idle runs and Drive-synced deployments
        cost zero rewrites.
        """
        stored = self._load()
        kept: dict[str, Vector] = {}
        library: dict[str, Vector] = {}
        witness: dict[str, Path] = {}
        computed = False
        for fandom, path in _tree(root, self._cap):
            ident = _identify(path, witness)
            vec = stored.get(ident)
            if vec is None:
                vec = kept.get(ident)
            if vec is None:
                vec = embed(path)
                computed = True
            kept[ident] = vec
            library[f'{fandom}|{path.name}'] = vec
        if computed or kept.keys() != stored.keys():
            self._save(kept)
        return library

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


def _identify(path: Path, witness: dict[str, Path]) -> str:
    """The verified cache key for one live file.

    ``witness`` maps each key to the first live file that claimed it
    this scan. A repeated key is settled by comparing the bytes
    themselves: identical -> a true duplicate, share the key (and
    the vector); different -> a detected hash collision, salted onto
    its own key so a wrong vector can never be served.
    """
    data = path.read_bytes()
    ident = f'{hashlib.sha256(data).hexdigest()}:{len(data)}'
    holder = witness.get(ident)
    if holder is None:
        witness[ident] = path
        return ident
    if holder.read_bytes() == data:
        return ident
    _LOG.critical('hash_collision a=%s b=%s', holder, path)
    return f'{ident}:{path.name}'


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


def warm_detector() -> None:
    """Load the person-box detector at process start (restore).

    Resources come up at init, never mid-flight: the first photo
    must not pay the model-load latency and memory spike.
    """
    _load_detection(_BOXES)


def warm_masks() -> None:
    """Load the person-segmentation model at start (censor-blur)."""
    _load_detection(_MASKS)


def warm_faces() -> None:
    """Load the face detector at start (censor-black)."""
    _mtcnn()


def warm_embedder() -> None:
    """Load CLIP at process start (same init-not-mid-flight rule)."""
    _clip()


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


_BOXES = 'fasterrcnn_resnet50_fpn'
_MASKS = 'maskrcnn_resnet50_fpn'

_DETECTORS: dict[str, Any] = {}
"""Loaded torchvision detection models, one per constructor name.

The cache is a plain dict (not a typed factory) so no function
returns the untyped vendor model -- keeping the module free of
``Any``-return waivers. Populated once at warm-up, read on each photo.
"""


def _load_detection(ctor: str) -> None:
    """Load a torchvision detection model into the cache (once)."""
    if ctor in _DETECTORS:
        return
    from torchvision.models import detection

    model = getattr(detection, ctor)(weights='DEFAULT')
    model.eval()
    _DETECTORS[ctor] = model


def person_boxes(path: Path) -> tuple[Box, ...]:
    """Bounding boxes of detected people (lazy torchvision load)."""
    import torch
    from torchvision.transforms import functional as tvf

    _load_detection(_BOXES)
    tensor = tvf.to_tensor(load_rgb(path))
    with torch.no_grad():
        found = _DETECTORS[_BOXES]([tensor])[0]
    return _to_boxes(found)


def person_masks(path: Path) -> Mask | None:
    """A union L-mask of every confident person, or None (lazy load).

    Instance segmentation (Mask R-CNN) so censor-blur can blur the
    person's silhouette instead of a full bounding box. numpy builds
    the L bytes here; Pillow composites them in files.blur_masked.
    """
    import torch
    from torchvision.transforms import functional as tvf

    _load_detection(_MASKS)
    tensor = tvf.to_tensor(load_rgb(path))
    with torch.no_grad():
        found = _DETECTORS[_MASKS]([tensor])[0]
    return _union_mask(found)


def _union_mask(found: dict[str, Any]) -> Mask | None:
    """Union the confident person masks into one L-mode alpha."""
    union: Vector | None = None
    for label, score, mask in zip(
        found['labels'], found['scores'], found['masks'], strict=True
    ):
        if int(label) != 1 or float(score) < PERSON_SCORE_MIN:
            continue
        binary = mask[0].numpy() >= MASK_THRESHOLD
        union = binary if union is None else (union | binary)
    if union is None:
        return None
    height, width = union.shape
    data = (union.astype(np.uint8) * 255).tobytes()
    return Mask(width=int(width), height=int(height), data=data)


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


def _s1(job: Job) -> Path:
    """The intermediate the censor family writes (OPERATIONS 6)."""
    return job.dest / f'{job.src.stem}_s1{job.src.suffix}'


class HideFaces(Step):
    """censor-black: black out each detected face (CT-B).

    Correctness is CT-B: a missed face leaks identity, so boxes apply
    verbatim and zero detections is a SKIP, never a pass-through of
    the original. Faces, not the whole person: a portrait keeps its
    scene, only the face goes dark.
    """

    def process(self, job: Job) -> Verdict:
        """Detect faces and black them; the ``_s1`` copy is the result."""
        boxes = face_boxes(job.src)
        if not boxes:
            return Verdict(
                Disposition.SKIPPED, reason='no_face', reply='no faces found'
            )
        out = _s1(job)
        hide_boxes(job.src, out, HideSpec(boxes=boxes, mode=BLACK))
        return Verdict(Disposition.DELIVERED, result=out, reply='censored')


class BlurContour(Step):
    """censor-blur: blur each person's silhouette (CT-B).

    Segmentation, not a box: only the person is blurred, the scene
    behind stays sharp. Zero detections is a SKIP (never a
    pass-through of the original).
    """

    def process(self, job: Job) -> Verdict:
        """Segment people and blur them; the ``_s1`` copy is the result."""
        mask = person_masks(job.src)
        if mask is None:
            return Verdict(
                Disposition.SKIPPED,
                reason='no_person',
                reply='no people found',
            )
        out = _s1(job)
        blur_masked(job.src, out, mask)
        return Verdict(Disposition.DELIVERED, result=out, reply='censored')


class HidePersonBoxes(Step):
    """restore step 1: blur whole-person boxes for the LLM to erase.

    restore only needs the people marked so the repaint model removes
    them; a person box (not just the face) covers the whole subject.
    Zero detections is a SKIP (CT-B).
    """

    def process(self, job: Job) -> Verdict:
        """Blur every person box; the ``_s1`` copy is the result."""
        boxes = person_boxes(job.src)
        if not boxes:
            return Verdict(
                Disposition.SKIPPED,
                reason='no_person',
                reply='no people found',
            )
        out = _s1(job)
        hide_boxes(job.src, out, HideSpec(boxes=boxes, mode=BLUR))
        return Verdict(Disposition.DELIVERED, result=out, reply='censored')
