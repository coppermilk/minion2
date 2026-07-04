"""Weekly script boundary: Google Docs text for the classify hint.

Second sanctioned ``requests`` import site next to tg.py
(REQ-ARC-002, BLUEPRINT 11). The weekly scripts arrive as ``.gdoc``
shortcuts dropped into ``_inbox/`` (the docs are shared "anyone with
the link", so the plain export URL works -- no credentials, no
hardcoded document ids). A shortcut is consumed -- fetched then
deleted -- by the first run that sees it, so the fetched text is
archived under ``Scripts/`` where every later run (sort on the NAS,
catch on Windows) reads it for the rest of the week.

Every failure degrades to an empty hint: classification proceeds
without scene labels rather than stalling the belt (REQ-DEG-001
spirit).
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from minion_core.adapters.files import atomic_write
from minion_core.adapters.files import stem

if TYPE_CHECKING:
    from pathlib import Path

    from minion_core.settings import Settings

_LOG = logging.getLogger('scripts')

MAX_SCRIPT_CHARS = 6_000
"""Cap on injected script text (bounded prompt, BLUEPRINT 4)."""

FETCH_TIMEOUT_SEC = 15
"""One short attempt per document; failures degrade to ''."""

_EXPORT_URL = 'https://docs.google.com/document/d/{id}/export?format=txt'

_DOC_ID = re.compile(r'/document/d/([a-zA-Z0-9_-]+)')

_ID_OK = re.compile(r'^[A-Za-z0-9_-]+$')
"""A Drive document id is opaque base64url; nothing else may reach
the export URL (a stray ``/`` or ``?`` would let a crafted shortcut
retarget the request)."""


def script_hint(cfg: Settings) -> str:
    """This week's script text, or '' when there is none.

    Fresh ``.gdoc`` shortcuts in the inbox win and are archived;
    otherwise the newest archived script is served.
    """
    text = read_scripts_from_inbox(cfg.inbox)
    if text:
        name = stem('script', 'doc') + '.txt'
        atomic_write(cfg.scripts / name, text.encode('utf-8'))
        return text
    return _newest_archived(cfg.scripts)


def read_scripts_from_inbox(inbox: Path) -> str:
    """Consume every ``.gdoc`` shortcut: fetch its text, delete it.

    Several shortcuts at once are combined with blank lines; a
    shortcut is deleted even when its fetch fails (a dead shortcut
    must not wedge every later run).
    """
    if not inbox.is_dir():
        return ''
    texts: list[str] = []
    for shortcut in sorted(inbox.glob('*.gdoc')):
        doc_id = _id_from_gdoc(shortcut)
        if not doc_id:
            _LOG.warning('script_skipped reason=no_doc_id src=%s', shortcut)
        else:
            text = read_script_doc(doc_id)
            if text:
                texts.append(text)
        shortcut.unlink(missing_ok=True)
        _LOG.info('script_consumed src=%s', shortcut.name)
    return '\n\n'.join(texts)


def read_script_doc(doc_id: str) -> str:
    """Fetch one document's plain-text export; '' on any failure."""
    if not doc_id:
        return ''
    match = _DOC_ID.search(doc_id)
    doc_id = match.group(1) if match else doc_id.strip()
    if not _ID_OK.match(doc_id):
        _LOG.warning('script_skipped reason=bad_doc_id')
        return ''
    return _fetch_text(_EXPORT_URL.format(id=doc_id))


def _fetch_text(url: str) -> str:
    import requests

    try:
        # No redirects: the export URL is fixed, so a 3xx can only be
        # an attempt to bounce this credential-less fetch somewhere
        # unintended (e.g. an SSRF pivot). Treat it as a failure.
        resp = requests.get(
            url, timeout=FETCH_TIMEOUT_SEC, allow_redirects=False
        )
        resp.raise_for_status()
    except (requests.RequestException, OSError) as exc:
        _LOG.warning('script_fetch_failed reason=%s', exc)
        return ''
    kind = resp.headers.get('Content-Type', '')
    if 'text/plain' not in kind:
        _LOG.warning('script_fetch_failed reason=content_type %s', kind)
        return ''
    text: str = resp.text.strip()
    if len(text) > MAX_SCRIPT_CHARS:
        _LOG.info('script_truncated chars=%d', MAX_SCRIPT_CHARS)
        text = text[:MAX_SCRIPT_CHARS]
    return text


def _id_from_gdoc(path: Path) -> str:
    """The document id inside a Drive ``.gdoc`` shortcut (JSON)."""
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return ''
    if not isinstance(data, dict):
        return ''
    doc_id = data.get('doc_id', '')
    if isinstance(doc_id, str) and doc_id:
        return doc_id
    url = data.get('url', '')
    match = _DOC_ID.search(url) if isinstance(url, str) else None
    return match.group(1) if match else ''


def _newest_archived(archive: Path) -> str:
    """The most recently modified archived script, or ''."""
    if not archive.is_dir():
        return ''
    candidates = sorted(
        archive.glob('*.txt'),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        if text.strip():
            return text.strip()[:MAX_SCRIPT_CHARS]
    return ''
