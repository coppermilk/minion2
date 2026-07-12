"""censor-blur: blur each person's silhouette. This minion knows only this.

Owns its model (Mask R-CNN instance segmentation) and its blur Step. Imports
only the imaging framework (Pillow via ``files``) and torch/torchvision as
SDKs -- no sibling service, no catalog, no Telegram. Heavy vendors load lazily
on the first photo (Power-of-10 rule 3); numpy is the mask format.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from typing import TypeAlias

import numpy as np

from minion_core.adapters.files import Mask
from minion_core.adapters.files import blur_masked
from minion_core.adapters.files import load_rgb
from minion_core.kernel import Disposition
from minion_core.kernel import Step
from minion_core.kernel import Verdict

if TYPE_CHECKING:
    from pathlib import Path

    from minion_core.kernel import Job

Vector: TypeAlias = 'np.ndarray[Any, Any]'

PERSON_SCORE_MIN = 0.7
"""Detection confidence below which a person mask is noise."""

MASK_THRESHOLD = 0.5
"""Per-pixel probability above which a mask covers the person."""

_MASKS = 'maskrcnn_resnet50_fpn'
_MODEL: dict[str, Any] = {}
"""The loaded torchvision model, cached once and read on each photo."""


def _load() -> None:
    """Load the segmentation model into the cache (once)."""
    if _MASKS in _MODEL:
        return
    from torchvision.models import detection

    model = getattr(detection, _MASKS)(weights='DEFAULT')
    model.eval()
    _MODEL[_MASKS] = model


def person_masks(path: Path) -> Mask | None:
    """A union L-mask of every confident person, or None (lazy load)."""
    import torch
    from torchvision.transforms import functional as tvf

    _load()
    tensor = tvf.to_tensor(load_rgb(path))
    with torch.no_grad():
        found = _MODEL[_MASKS]([tensor])[0]
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


def _s1(job: Job) -> Path:
    """The intermediate the censor step writes (OPERATIONS 6)."""
    return job.dest / f'{job.src.stem}_s1{job.src.suffix}'


class BlurContour(Step):
    """censor-blur: blur each person's silhouette (CT-B).

    Segmentation, not a box: only the person is blurred, the scene behind
    stays sharp. Zero detections is a SKIP (never a pass-through).
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
