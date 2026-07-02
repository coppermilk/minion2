"""Filesystem boundary: naming, EXIF week tag, lock, quota, delivery.

Vendor SDKs owned here: Pillow, piexif -- this file is their sole
importer (REQ-ARC-002). This is the base adapter: other adapters may
import it (quota accounting, budgeted writes).

Re-exports (the documented API of REQ-DATA-001/002):
``next_free_path``, ``atomic_write``, ``move_atomic`` -- implemented
in the kernel because its sinks rely on them; see the kernel
placement note.
"""

from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING
from typing import Any

from minion_core.kernel import Disposition
from minion_core.kernel import Step
from minion_core.kernel import Verdict
from minion_core.kernel import atomic_write
from minion_core.kernel import move_atomic
from minion_core.kernel import next_free_path

if TYPE_CHECKING:
    from pathlib import Path

    from minion_core.kernel import Job
    from minion_core.settings import Settings

__all__ = [
    'BLACK',
    'BLUR',
    'BatchLock',
    'BudgetWriter',
    'Deliver',
    'HideSpec',
    'QuotaExceeded',
    'atomic_write',
    'free_quota',
    'has_week',
    'hide_boxes',
    'load_rgb',
    'move_atomic',
    'next_free_path',
    'sanitize',
    'stem',
    'strip_week',
    'tag_week',
    'used_bytes',
    'valid_image',
]

NAME_MAX = 80
"""Longest sanitized name fragment kept (bounded names)."""

_UNSAFE = re.compile(r'[^A-Za-z0-9._-]+')
_JPEG = ('.jpg', '.jpeg')


class QuotaExceeded(Exception):
    """Disk budget exhausted; reason code ``quota_exceeded``."""


def sanitize(name: str) -> str:
    """Reduce untrusted names to a safe ASCII fragment."""
    safe = _UNSAFE.sub('_', name).strip('._-')
    return safe[:NAME_MAX] or 'item'


def stem(name: str, source: str, when: date | None = None) -> str:
    """Canonical stem ``MMDD_<source>_<name>`` (OPERATIONS 6)."""
    day = date.today() if when is None else when  # noqa: DTZ011 -- local date is the naming intent
    return f'{day:%m%d}_{source}_{sanitize(name)}'


def used_bytes(root: Path) -> int:
    """Bytes currently under the media tree (quota accounting)."""
    files = (p for p in root.rglob('*') if p.is_file())
    return sum(p.stat().st_size for p in files)


def free_quota(cfg: Settings) -> int:
    """Remaining byte budget under ``quota_bytes`` (REQ-RES-002)."""
    return cfg.quota_bytes - used_bytes(cfg.drive)


class BudgetWriter:
    """Stream bytes to a temp file, aborting past the byte budget.

    The mid-stream half of the two-sided quota check (REQ-RES-002);
    ``commit`` renames atomically (REQ-DATA-002), ``abort`` removes
    the partial file.
    """

    def __init__(self, target: Path, budget: int) -> None:
        self._target = target
        self._budget = budget
        self._tmp = target.with_name(target.name + '.part')
        self._written = 0
        target.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._tmp.open('wb')

    def write(self, chunk: bytes) -> None:
        """Append a chunk; raise past the budget (mid-stream)."""
        self._written += len(chunk)
        if self._written > self._budget:
            self.abort()
            raise QuotaExceeded(f'quota_exceeded: {self._target.name}')
        self._fh.write(chunk)

    def commit(self) -> Path:
        """Finish: atomic rename into place."""
        self._fh.close()
        self._tmp.replace(self._target)
        return self._target

    def abort(self) -> None:
        """Discard the partial file."""
        self._fh.close()
        self._tmp.unlink(missing_ok=True)


class BatchLock:
    """One run at a time per batch bot (REQ-RES-003).

    O_CREAT|O_EXCL with ``host:pid`` inside. A lock left by a dead
    process on THIS host is reaped, so a crash cannot wedge the
    schedule; a foreign host's lock is never touched -- pid liveness
    is meaningless across pid namespaces, and stealing a live lock
    would break the no-overlap guarantee. An orphaned foreign lock
    (its container was recreated, not restarted) is removed by hand
    (OPERATIONS 2, ``batch_locked``).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._held = False

    def acquire(self) -> bool:
        """Take the lock; False means another run is live."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._reap()
        try:
            fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, 'w', encoding='ascii') as fh:
            fh.write(f'{socket.gethostname()}:{os.getpid()}')
        self._held = True
        return True

    def release(self) -> None:
        """Drop the lock if held."""
        if self._held:
            self._path.unlink(missing_ok=True)
            self._held = False

    def _reap(self) -> None:
        try:
            host, _, raw_pid = self._path.read_text(
                encoding='ascii'
            ).partition(':')
            pid = int(raw_pid)
        except (OSError, ValueError):
            return
        if host != socket.gethostname():
            return  # foreign holder: never steal across namespaces
        if not _alive(pid):
            self._path.unlink(missing_ok=True)


def _alive(pid: int) -> bool:
    """Whether a pid names a live process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


BLUR = 'blur'
"""Hide mode: Gaussian blur over the region (censor-blur bot)."""

BLACK = 'black'
"""Hide mode: solid rectangle over the region (censor-black bot)."""


@dataclass(frozen=True)
class HideSpec:
    """Regions to hide and how (the censor family, BLUEPRINT 9)."""

    boxes: tuple[tuple[int, int, int, int], ...]
    mode: str


BLUR_RADIUS = 24
"""Gaussian radius strong enough to hide identity."""


def hide_boxes(src: Path, out: Path, spec: HideSpec) -> Path:
    """Write a copy of ``src`` with every box hidden (CT-B).

    A missed region leaks the hidden subject, so boxes are applied
    verbatim -- no shrinking, no heuristics.
    """
    from PIL import Image
    from PIL import ImageDraw
    from PIL import ImageFilter

    with Image.open(src) as opened:
        img = opened.convert('RGB')
    for box in spec.boxes:
        if spec.mode == BLACK:
            ImageDraw.Draw(img).rectangle(box, fill=(0, 0, 0))
        else:
            region = img.crop(box)
            blurred = region.filter(
                ImageFilter.GaussianBlur(BLUR_RADIUS),
            )
            img.paste(blurred, (box[0], box[1]))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def load_rgb(path: Path) -> Any:  # noqa: ANN401 -- opaque vendor handle
    """Open an image as an RGB Pillow handle.

    Pillow stays behind this file (REQ-ARC-002); other adapters take
    the handle opaquely.
    """
    from PIL import Image

    with Image.open(path) as img:
        return img.convert('RGB')


def valid_image(path: Path) -> bool:
    """Validate untrusted image bytes explicitly (BLUEPRINT 4)."""
    from PIL import Image

    try:
        with Image.open(path) as img:
            img.verify()
    except (OSError, SyntaxError):
        return False
    return True


def tag_week(path: Path, tag: str) -> None:
    """Write the weekly EXIF tag into a JPEG (no-op otherwise)."""
    import piexif

    if path.suffix.lower() not in _JPEG:
        return
    exif = piexif.load(str(path))
    comment = b'ASCII\x00\x00\x00' + tag.encode('ascii')
    exif['Exif'][piexif.ExifIFD.UserComment] = comment
    piexif.insert(piexif.dump(exif), str(path))


def has_week(path: Path, tag: str) -> bool:
    """Whether the JPEG carries the weekly tag."""
    import piexif

    if path.suffix.lower() not in _JPEG:
        return False
    exif = piexif.load(str(path))
    raw = exif['Exif'].get(piexif.ExifIFD.UserComment, b'')
    return bool(raw.endswith(tag.encode('ascii')))


def strip_week(path: Path, tag: str) -> None:
    """Remove the weekly EXIF tag if present."""
    import piexif

    if not has_week(path, tag):
        return
    exif = piexif.load(str(path))
    del exif['Exif'][piexif.ExifIFD.UserComment]
    piexif.insert(piexif.dump(exif), str(path))


@dataclass(frozen=True)
class Deliver(Step):
    """Move ``job.src`` into ``job.dest`` under the canonical stem."""

    def process(self, job: Job) -> Verdict:
        """Deliver collision-free and atomically (REQ-DATA-001/002)."""
        if not job.src.is_file():
            return Verdict(Disposition.REJECTED, reason='missing_input')
        name = stem(job.stem, job.origin.source) + job.src.suffix.lower()
        target = next_free_path(job.dest / name)
        moved = move_atomic(job.src, target)
        return Verdict(
            Disposition.DELIVERED, result=moved, reply=f'saved {moved.name}'
        )
