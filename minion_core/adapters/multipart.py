# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Streaming multipart/form-data body: upload a file without buffering it.

requests' ``files=`` encoder calls ``fp.read()`` and assembles the whole
multipart body in memory, which OOMs on a large video (seen in the telegram
container). ``StreamMultipart`` yields the envelope and the file in
CHUNK-sized pieces and reports its total length via ``__len__``, so requests
sets ``Content-Length`` and streams the iterable rather than buffering it or
falling back to chunked transfer encoding. Pure bytes -- it does NOT import
``requests`` -- so every sanctioned HTTP site (tg, service_call) can share it
without widening the requests surface (REQ-ARC-002).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Iterator
    from collections.abc import Mapping
    from pathlib import Path

CHUNK = 64 * 1024
"""Streaming read size for the file part (bounded memory, BLUEPRINT 10)."""


@dataclass(frozen=True)
class FilePart:
    """The one file field of a multipart body: its field name and source.

    ``filename`` is the sender's name (untrusted); it is sanitized into the
    header. ``path`` is the spooled, immutable source read in chunks.
    """

    field: str
    filename: str
    path: Path


class StreamMultipart:
    """A multipart/form-data body streamed from disk, never read whole.

    One file field plus any number of text fields. The source file is assumed
    immutable between the length read (``__len__``) and the stream
    (``__iter__``) -- true for a spooled file -- so the declared
    Content-Length always matches the bytes sent.
    """

    def __init__(self, fields: Mapping[str, str], file: FilePart) -> None:
        """Precompute the envelope around the one file part."""
        self._path = file.path
        self._boundary = uuid4().hex
        # Strip quote/CR/LF from the untrusted filename (header injection).
        name = file.filename.translate({0x22: None, 0x0D: None, 0x0A: None})
        dash = f'--{self._boundary}\r\n'
        head = ''.join(
            f'{dash}Content-Disposition: form-data; name="{key}"\r\n\r\n'
            f'{value}\r\n'
            for key, value in fields.items()
        )
        head += (
            f'{dash}Content-Disposition: form-data; name="{file.field}"; '
            f'filename="{name}"\r\n'
            f'Content-Type: application/octet-stream\r\n\r\n'
        )
        self._head = head.encode()
        self._tail = f'\r\n--{self._boundary}--\r\n'.encode('ascii')

    @property
    def content_type(self) -> str:
        """The multipart Content-Type header naming this body's boundary."""
        return f'multipart/form-data; boundary={self._boundary}'

    def __len__(self) -> int:
        """Total body length, so requests sends Content-Length, not chunked."""
        return len(self._head) + self._path.stat().st_size + len(self._tail)

    def __iter__(self) -> Iterator[bytes]:
        """Yield the envelope and the file in CHUNK pieces (bounded memory)."""
        yield self._head
        with self._path.open('rb') as fh:
            while chunk := fh.read(CHUNK):
                yield chunk
        yield self._tail
