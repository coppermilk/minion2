"""restore step 1: blur whole-person boxes for the LLM to erase.

Owns its model (Faster R-CNN person detection) and its mark Step. Imports only
the imaging framework (Pillow via ``files``) and torch/torchvision as SDKs.
The LLM repaint that follows (step 2) is the shared Gemini adapter, wired in
this minion's ``service.py`` -- not here. No sibling service, no Telegram.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from typing import TypeAlias

from minion_core.adapters.files import BLUR
from minion_core.adapters.files import HideSpec
from minion_core.adapters.files import hide_boxes
from minion_core.adapters.files import load_rgb
from minion_core.kernel import Disposition
from minion_core.kernel import Step
from minion_core.kernel import Verdict

if TYPE_CHECKING:
    from pathlib import Path

    from minion_core.kernel import Job

Box: TypeAlias = tuple[int, int, int, int]

PERSON_SCORE_MIN = 0.7
"""Detection confidence below which a person box is noise."""

_BOXES = 'fasterrcnn_resnet50_fpn'
_MODEL: dict[str, Any] = {}
"""The loaded torchvision model, cached once and read on each photo."""


def _load() -> None:
    """Load the detection model into the cache (once)."""
    if _BOXES in _MODEL:
        return
    from torchvision.models import detection

    model = getattr(detection, _BOXES)(weights='DEFAULT')
    model.eval()
    _MODEL[_BOXES] = model


def person_boxes(path: Path) -> tuple[Box, ...]:
    """Bounding boxes of detected people (lazy torchvision load)."""
    import torch
    from torchvision.transforms import functional as tvf

    _load()
    tensor = tvf.to_tensor(load_rgb(path))
    with torch.no_grad():
        found = _MODEL[_BOXES]([tensor])[0]
    return _to_boxes(found)


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


def _s1(job: Job) -> Path:
    """The intermediate the mark step writes (OPERATIONS 6)."""
    return job.dest / f'{job.src.stem}_s1{job.src.suffix}'


class HidePersonBoxes(Step):
    """restore step 1: blur whole-person boxes for the LLM to erase.

    A person box (not just the face) covers the whole subject so the repaint
    model removes it. Zero detections is a SKIP (CT-B).
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
