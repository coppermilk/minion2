"""Vision boundary: CLIP embeddings and nearest-fandom retrieval.

The detection models (person masks/boxes, faces) and the censor Steps that use
them now live in their own service minions (``minions/censor_blur``,
``minions/censor_black``, ``minions/restore``) -- each owns its model. What
stays here is the CLIP embedding half that sort/props/catch use.

Heavy vendors (torch, transformers) load lazily inside the functions that use
them, so a caller needing no ML -- and the test suite -- never loads them
(Power-of-10 rule 3). numpy is the cache format and loads eagerly. The
embedding cache is CACHE-class data (BLUEPRINT 1.2): disposable, rebuilds
unattended, wiped by Demote (REQ-SORT-001).
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

from minion_core.adapters.files import load_rgb
from minion_core.settings import UNKNOWN

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterator

    from minion_core.settings import Settings

_LOG = logging.getLogger('vision')

Vector: TypeAlias = 'np.ndarray[Any, Any]'
Embedder: TypeAlias = 'Callable[[Path], Vector]'

IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.webp')
"""Image suffixes the vision passes consider."""


class EmbeddingCache:
    """Append-only vectors in regen/, keyed by verified content.

    The stored key is ``SHA-256:byte-length``. Identity is never taken on
    faith among coexisting files: when two live files share a key, their
    bytes are compared directly -- equal bytes share one vector (a verified
    duplicate), unequal bytes are a detected collision, split onto distinct
    keys with a CRITICAL log. That makes wrong-vector service impossible for
    anything that can be checked.

    An image is embedded exactly once in its life; moves and renames reuse
    the vector. The fandom mapping is rebuilt from the live tree on every
    refresh and never persisted (REQ-SORT-001). The scan is capped.
    """

    def __init__(self, cfg: Settings) -> None:
        self._file = cfg.regen / '_embeddings.npz'
        self._cap = cfg.max_embedding_scan

    def invalidate(self) -> None:
        """Drop the cache -- a manual recovery tool (CACHE class)."""
        self._file.unlink(missing_ok=True)

    def refresh(self, root: Path, embed: Embedder) -> dict[str, Vector]:
        """Return the live ``fandom|name -> vector`` library.

        Embeds only identities never seen before; an unchanged tree writes
        nothing, so idle runs and Drive-synced deployments cost zero rewrites.
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

    ``witness`` maps each key to the first live file that claimed it this
    scan. A repeated key is settled by comparing the bytes themselves:
    identical -> a true duplicate, share the key; different -> a detected
    hash collision, salted onto its own key so a wrong vector is never served.
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


def nearest_named(
    vec: Vector, library: dict[str, Vector]
) -> tuple[str, float]:
    """The library key most similar to ``vec`` and its cosine.

    Keeps the full key and the score (unlike ``nearest_fandom``), so a caller
    can threshold on similarity -- the props bot's have/need split. An empty
    library returns ``('', -2.0)``.
    """
    best_key = ''
    best_sim = -2.0
    for key, other in library.items():
        sim = _cosine(vec, other)
        if sim > best_sim:
            best_sim = sim
            best_key = key
    return best_key, best_sim


def nearest_fandom(
    vec: Vector, library: dict[str, Vector], tau: float = 0.0
) -> str | None:
    """The fandom of the most similar library vector above ``tau``.

    ``tau`` is the minimum cosine to accept a match. Below it the image
    is too unlike anything in the library to belong to a fandom, so it
    returns None (-> Unknown) instead of being forced into the nearest
    one -- the difference between "this is Harry Potter" and "this is the
    closest of several fandoms, none of them close". tau=0.0 keeps the
    old always-match behaviour.
    """
    best_key, best_sim = nearest_named(vec, library)
    if not best_key or best_sim < tau:
        return None
    return best_key.split('|', 1)[0]


def _cosine(a: Vector, b: Vector) -> float:
    norm = float(np.linalg.norm(a) * np.linalg.norm(b))
    if norm == 0.0:
        return -1.0
    return float(np.dot(a, b) / norm)


def warm_embedder() -> None:
    """Load CLIP at process start (resources at init, never mid-flight)."""
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


def _pool1d(vec: Vector) -> Vector:
    """Reduce a stray multi-dim embedding to a 1-D vector.

    Some transformers builds return per-token features from ``get_*_features``
    instead of a pooled vector, which breaks cosine. Mean-pool the leading
    axes so every vector is 1-D; a healthy ``(dim,)`` is returned unchanged.
    """
    if vec.ndim <= 1:
        return vec
    pooled: Vector = vec.reshape(-1, vec.shape[-1]).mean(axis=0)
    return pooled


def embed_image(path: Path) -> Vector:
    """CLIP image embedding (lazy torch + transformers load)."""
    import torch

    model, processor = _clip()
    batch = processor(images=load_rgb(path), return_tensors='pt')
    with torch.no_grad():
        feats = model.get_image_features(**batch)
    return _pool1d(feats[0].numpy())


def embed_text(query: str) -> Vector:
    """CLIP text embedding, in the same space as ``embed_image``."""
    import torch

    model, processor = _clip()
    batch = processor(
        text=[query], return_tensors='pt', padding=True, truncation=True
    )
    with torch.no_grad():
        feats = model.get_text_features(**batch)
    return _pool1d(feats[0].numpy())
