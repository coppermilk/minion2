# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Trackable short links for the post, computed offline.

The site's link shortener (the ``go`` worker of daetotyoma.com) names every
video's short link as a pure function of ``<network>:<id>``: the first seven
bytes of SHA-256 over that string, each mapped onto a 31-letter alphabet
without the look-alike glyphs ``0 1 i l o``. The public address is then
``https://<network>.<domain>/<code>``. Because the name is a function and
not a database row, the poster can produce the very same address here, with
no network call and no secret -- the worker recognises a code it has never
stored by hashing its own feeds the same way.

Only the four video networks are rewritten (tiktok, youtube, instagram,
pinterest). Anything else -- an intermediary short link such as
``vm.tiktok.com``, a channel, a site -- is left exactly as it came.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qs
from urllib.parse import urlsplit

# 31 letters, no 0/1/i/l/o: the same alphabet as the worker's ``shortHash``.
ALPHABET = 'abcdefghjkmnpqrstuvwxyz23456789'
# Letters of a video's code; the worker lengthens it only on a collision.
CODE_LEN = 7

_TIKTOK_HOST = re.compile(r'(^|\.)tiktok\.com$')
_TIKTOK_PATH = re.compile(r'/video/(\d{6,})')
_YOUTUBE_HOST = re.compile(r'(^|\.)youtube(-nocookie)?\.com$')
_YOUTUBE_PATH = re.compile(r'^/(?:shorts|embed|live|v)/([\w-]{11})')
_YOUTUBE_ID = re.compile(r'^[\w-]{11}$')
_INSTAGRAM_HOST = re.compile(r'(^|\.)instagram\.com$')
_INSTAGRAM_PATH = re.compile(r'^/(?:reels?|p|tv)/([\w-]+)')
_PINTEREST_HOST = re.compile(r'(^|\.)pinterest\.[a-z.]+$')
_PINTEREST_PATH = re.compile(r'^/pin/(\d{6,})')


def _host_path(url: str) -> tuple[str, str, str]:
    """Return (host without www./m., path, query) of a URL, or empties."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return '', '', ''
    host = re.sub(r'^(www|m)\.', '', (parts.hostname or '').lower())
    return host, parts.path, parts.query


def _youtube_id(path: str, query: str) -> str:
    """Return the 11-letter video id from a youtube.com URL, or ''."""
    found = _YOUTUBE_PATH.match(path)
    if found:
        return found.group(1)
    if not path.startswith('/watch'):
        return ''
    video = parse_qs(query).get('v', [''])[0]
    return video if _YOUTUBE_ID.match(video) else ''


def source_of(url: str) -> str:
    """Return the shortener's key ``<network>:<id>`` for a video URL, or ''.

    Mirrors the worker's ``parseTarget``: the same hosts, the same id
    shapes. An address it does not recognise as one of the four networks'
    videos yields '' -- the caller then keeps the URL as it came.
    """
    host, path, query = _host_path(url)
    if not host:
        return ''
    if _TIKTOK_HOST.search(host):
        found = _TIKTOK_PATH.search(path)
        return f'tiktok:{found.group(1)}' if found else ''
    if host == 'youtu.be':
        found = _YOUTUBE_ID.match(path.lstrip('/')[:11])
        return f'youtube:{found.group(0)}' if found else ''
    if _YOUTUBE_HOST.search(host):
        video = _youtube_id(path, query)
        return f'youtube:{video}' if video else ''
    return _source_of_photo_networks(host, path)


def _source_of_photo_networks(host: str, path: str) -> str:
    """Resolve the instagram / pinterest half of ``source_of``."""
    if _INSTAGRAM_HOST.search(host):
        found = _INSTAGRAM_PATH.match(path)
        return f'instagram:{found.group(1)}' if found else ''
    if _PINTEREST_HOST.search(host):
        found = _PINTEREST_PATH.match(path)
        return f'pinterest:{found.group(1)}' if found else ''
    return ''


def code_of(source: str, length: int = CODE_LEN) -> str:
    """Return the short code of a ``<network>:<id>`` key (worker formula)."""
    digest = hashlib.sha256(source.encode('utf-8')).digest()
    return ''.join(ALPHABET[byte % len(ALPHABET)] for byte in digest[:length])


def short_url(url: str, domain: str) -> str:
    """Return the trackable short address of a video URL, else the URL.

    ``domain`` is the link domain (``daetotyoma.com``); empty switches the
    rewrite off and every URL passes through untouched.
    """
    source = source_of(url) if domain else ''
    if not source:
        return url
    network = source.partition(':')[0]
    return f'https://{network}.{domain}/{code_of(source)}'
