# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Team-board boundary: read notes and post a reply over the site's MCP.

A sanctioned ``requests`` import site (REQ-ARC-002). The site's board is
reached through its MCP endpoint (JSON-RPC 2.0 over POST) with a role's
bearer token; the server signs every note with that role, so this door
cannot write under another name. Only three calls are used, on purpose:
``board`` to read notes, the same with ``q`` to find approvals, and
``board_post`` to answer in a thread -- nothing that changes a task.

Unlike the Telegram door, a failure here RAISES ``BoardError`` with the
call and the reason: the caller is a background worker that logs and
retries, and an edit must never go out on a board it could not read.
The token is never logged or put into an error.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

TIMEOUT_SEC = 30.0
"""Wall-time bound on one board call."""


class BoardError(RuntimeError):
    """A board call failed; the message names the call and the reason."""


@dataclass(frozen=True)
class BoardNote:
    """One note as the board returns it, the fields the editor reads."""

    id: int
    role: str
    kind: str
    text: str
    tags: tuple[str, ...]
    created_at: str
    parent_id: int = 0


@dataclass(frozen=True)
class Board:
    """The board behind ``url``, as the role that owns ``token``."""

    url: str
    token: str
    timeout: float = TIMEOUT_SEC

    def notes(self, **query: object) -> list[BoardNote]:
        """Return the notes matching ``query`` (the ``board`` tool's args)."""
        body = self._tool('board', dict(query))
        rows = body.get('notes')
        if not isinstance(rows, list):
            return []
        return [_note(row) for row in rows if isinstance(row, dict)]

    def reply(self, parent: int, text: str, ref: str) -> int:
        """Post ``text`` as a report in ``parent``'s thread; return its id.

        ``ref`` makes the post idempotent: the board does not duplicate a
        note with a ref it already holds, so a retried tick posts once.
        """
        args = {'parent': parent, 'kind': 'report', 'text': text, 'ref': ref}
        body = self._tool('board_post', args)
        note = body.get('note')
        got = note.get('id') if isinstance(note, dict) else body.get('id')
        return got if isinstance(got, int) else 0

    def _tool(self, name: str, args: dict[str, object]) -> dict[str, object]:
        """Call one MCP tool; return the JSON object its text carries."""
        import requests

        call = {
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'tools/call',
            'params': {'name': name, 'arguments': args},
        }
        try:
            resp = requests.post(
                self.url,
                data=json.dumps(call),
                headers={
                    'Authorization': f'Bearer {self.token}',
                    'Content-Type': 'application/json',
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            envelope = resp.json()
        except (requests.RequestException, ValueError) as exc:
            msg = f'board {name}: {type(exc).__name__}'
            raise BoardError(msg) from None
        return _payload(name, envelope)


def _payload(name: str, envelope: object) -> dict[str, object]:
    """Unwrap a JSON-RPC answer: the tool's text holds a JSON object.

    The board answers a summary line, then the JSON; everything before the
    first brace is that summary.
    """
    text = _text(name, envelope)
    start = text.find('{')
    try:
        body = json.loads(text[start:]) if start >= 0 else {}
    except json.JSONDecodeError as exc:
        msg = f'board {name}: answer is not JSON ({exc.msg})'
        raise BoardError(msg) from exc
    return body if isinstance(body, dict) else {}


def _text(name: str, envelope: object) -> str:
    """Return the tool's text; raise on a JSON-RPC or a tool error."""
    if not isinstance(envelope, dict):
        msg = f'board {name}: not a JSON-RPC answer'
        raise BoardError(msg)
    if 'error' in envelope:
        msg = f'board {name}: {_said(envelope["error"])}'
        raise BoardError(msg)
    result = envelope.get('result')
    result = result if isinstance(result, dict) else {}
    content = _first(result.get('content'))
    if result.get('isError'):
        msg = f'board {name}: {content[:200]}'
        raise BoardError(msg)
    return content


def _first(content: object) -> str:
    """Return the text of the first content block, '' when there is none."""
    if not isinstance(content, list) or not content:
        return ''
    first = content[0]
    return str(first.get('text', '')) if isinstance(first, dict) else ''


def _said(error: object) -> str:
    """Return a JSON-RPC error's message."""
    if isinstance(error, dict):
        return str(error.get('message', 'error'))[:200]
    return str(error)[:200]


def _note(row: Mapping[str, object]) -> BoardNote:
    """Read one note row."""
    tags = row.get('tags')
    ident, parent = row.get('id'), row.get('parent_id')
    return BoardNote(
        id=ident if isinstance(ident, int) else 0,
        role=str(row.get('role') or ''),
        kind=str(row.get('kind') or ''),
        text=str(row.get('text') or ''),
        tags=tuple(str(t) for t in tags) if isinstance(tags, list) else (),
        created_at=str(row.get('created_at') or ''),
        parent_id=parent if isinstance(parent, int) else 0,
    )
