"""censor-black: black out each detected face. This minion knows only this.

Owns its model (facenet MTCNN face detection) and its black-box Step. Imports
only the imaging framework (Pillow via ``files``) and facenet as an SDK -- no
sibling service, no catalog, no Telegram. The detector loads lazily on the
first photo (Power-of-10 rule 3).
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING
from typing import Any
from typing import TypeAlias

from minion_core.adapters.files import BLACK
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
    """The intermediate the censor step writes (OPERATIONS 6)."""
    return job.dest / f'{job.src.stem}_s1{job.src.suffix}'


class HideFaces(Step):
    """censor-black: black out each detected face (CT-B).

    A missed face leaks identity, so boxes apply verbatim and zero detections
    is a SKIP, never a pass-through. Faces, not the whole person: a portrait
    keeps its scene, only the face goes dark.
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
