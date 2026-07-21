"""Wishlist boundary: read a public Amazon list, spot what left it.

A daily snapshot of the wishlist is the database (STATE); an item that
was there yesterday and is gone today is treated as gifted. Third
sanctioned ``requests`` import site (REQ-ARC-002); the lazy import keeps
the module hermetic for the offline suite. Parsing a public list is
best-effort HTML scraping: a failed or blocked fetch (or any page of a
paginated list) returns ``None`` so the caller keeps the old snapshot
and never mistakes a block for a hundred gifts.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import unquote

from minion_core.adapters.files import atomic_write
from minion_core.adapters.tg import TgApi
from minion_core.adapters.tg import TgError

if TYPE_CHECKING:
    from pathlib import Path

_LOG = logging.getLogger('wishlist')

FETCH_TIMEOUT_SEC = 20
"""One short attempt per page; a failure keeps yesterday's snapshot."""

MAX_ITEMS = 500
"""Bound on items parsed from one page (bounded, BLUEPRINT 4)."""

MAX_PAGES = 25
"""Bound on lek-paginated pages walked in one run (bounded)."""

_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/122.0.0.0 Safari/537.36'
)

_HEADERS = {
    'User-Agent': _UA,
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.8',
    'Accept': 'text/html,application/xhtml+xml',
}

_ITEM_ID = re.compile(r'data-itemid="([^"]+)"')

_IMAGE = re.compile(
    r'https://[^"\'\s]+\.(?:media-amazon|ssl-images-amazon)'
    r'\.com/images/[^"\'\s]+'
)

_LEK = (
    re.compile(r'data-showmoreurl="[^"]*[?&]lek=([^"&]+)'),
    re.compile(r'"lastEvaluatedKey"\s*:\s*"([^"]+)"'),
    re.compile(r'id="[^"]*-lek"[^>]*value="([^"]+)"'),
)


@dataclass(frozen=True)
class WishItem:
    """One wishlist entry: id, title, photo URL and the owner's note."""

    ident: str
    title: str
    image: str
    note: str = ''


def _attr(window: str, tag: str, ident: str) -> str:
    """A ``title="..."`` value hanging off ``<tag>_<ident>``, unescaped."""
    match = re.search(
        r'id="' + tag + '_' + re.escape(ident) + r'"[^>]*?title="([^"]*)"',
        window,
    )
    return html.unescape(match.group(1)).strip() if match else ''


def _note(window: str, ident: str) -> str:
    """The owner's note for the item (``itemComment_<id>``), unescaped."""
    match = re.search(
        r'id="itemComment_' + re.escape(ident) + r'"[^>]*>([^<]*)<',
        window,
    )
    return html.unescape(match.group(1)).strip() if match else ''


def parse_items(page: str) -> list[WishItem]:
    """Every item found in the wishlist HTML (id, title, image, note).

    Each item's ``<li>`` carries ``data-itemid``; the window up to the
    next item holds that item's title anchor, product image and any
    note. A row without a title is skipped rather than guessed.
    """
    marks = list(_ITEM_ID.finditer(page))[:MAX_ITEMS]
    items: list[WishItem] = []
    seen: set[str] = set()
    for idx, mark in enumerate(marks):
        ident = mark.group(1)
        if ident in seen:
            continue
        end = marks[idx + 1].start() if idx + 1 < len(marks) else len(page)
        window = page[mark.start() : end]
        title = _attr(window, 'itemName', ident)
        if not title:
            continue
        image = _IMAGE.search(window)
        items.append(
            WishItem(
                ident=ident,
                title=title,
                image=image.group(0) if image else '',
                note=_note(window, ident),
            )
        )
        seen.add(ident)
    return items


def _next_lek(page: str) -> str | None:
    """The pagination token for the next page, or None when last."""
    for pattern in _LEK:
        found = pattern.search(page)
        if found:
            return unquote(found.group(1))
    return None


def _get_page(url: str, lek: str | None) -> str | None:
    """One page of the list HTML, or None on any fetch failure."""
    import requests

    params = {'lek': lek} if lek else None
    try:
        resp = requests.get(
            url,
            headers={**_HEADERS, 'Referer': url},
            params=params,
            timeout=FETCH_TIMEOUT_SEC,
        )
        resp.raise_for_status()
    except (requests.RequestException, OSError) as exc:
        _LOG.warning('wishlist_fetch_failed reason=%s', exc)
        return None
    if 'text/html' not in resp.headers.get('Content-Type', ''):
        _LOG.warning('wishlist_fetch_failed reason=not_html')
        return None
    page: str = resp.text
    return page


def _merge(items: list[WishItem], seen: set[str], page: str) -> None:
    """Append this page's not-yet-seen items to the running list."""
    for item in parse_items(page):
        if item.ident not in seen:
            seen.add(item.ident)
            items.append(item)


def fetch_items(url: str) -> list[WishItem] | None:
    """The whole wishlist across lek pages, or None on any failure.

    Any page that fails to fetch fails the whole run (None): a partial
    list would make the missing page's items look gifted next day. An
    empty parse is a failure too -- a live shared list has items, so
    zero means a block or a layout change, never a mass gifting.
    """
    items: list[WishItem] = []
    seen: set[str] = set()
    leks: set[str] = set()
    lek: str | None = None
    for _ in range(MAX_PAGES):
        page = _get_page(url, lek)
        if page is None:
            return None
        _merge(items, seen, page)
        lek = _next_lek(page)
        if not lek or lek in leks:
            break
        leks.add(lek)
    if not items:
        _LOG.warning('wishlist_parse_empty reason=blocked_or_layout')
        return None
    return items


def gifted(
    previous: list[WishItem], current: list[WishItem]
) -> list[WishItem]:
    """Items present yesterday but gone today -- the gifts."""
    have = {item.ident for item in current}
    return [item for item in previous if item.ident not in have]


@dataclass(frozen=True)
class SnapshotStore:
    """Yesterday's wishlist on disk (STATE, JSON; REQ-DATA-002)."""

    path: Path

    def load(self) -> list[WishItem]:
        """The last saved snapshot, or [] when there is none/bad."""
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return []
        rows = data if isinstance(data, list) else []
        out = [_row(r) for r in rows if isinstance(r, dict)]
        return [item for item in out if item is not None]

    def save(self, items: list[WishItem]) -> None:
        """Persist today's snapshot atomically (REQ-DATA-002)."""
        payload = [
            {
                'id': i.ident,
                'title': i.title,
                'image': i.image,
                'note': i.note,
            }
            for i in items
        ]
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        atomic_write(self.path, raw)


def _str(value: object) -> str:
    """A field coerced to str, '' when absent or not a string."""
    return value if isinstance(value, str) else ''


def _row(row: dict[str, object]) -> WishItem | None:
    """One snapshot row back into a WishItem, or None when id-less."""
    ident = row.get('id')
    if not isinstance(ident, str) or not ident:
        return None
    return WishItem(
        ident=ident,
        title=_str(row.get('title')),
        image=_str(row.get('image')),
        note=_str(row.get('note')),
    )


@dataclass(frozen=True)
class TgPhoto:
    """Post photos and text to a chat; text-only on photo fallback.

    A missing image, or a sendPhoto the Bot API refuses (a dead image
    URL), falls back to a plain message so the gift is still announced.
    """

    api: TgApi
    chat: str

    def post(self, image: str, caption: str) -> None:
        """Send the photo+caption, or the caption alone as a message."""
        if not self.api.live or not self.chat:
            return
        if image and self._photo(image, caption):
            return
        self.text(caption)

    def text(self, message: str) -> None:
        """Send one plain-text message; no-op without token or chat."""
        if not self.api.live or not self.chat:
            return
        self.api.call('sendMessage', {'chat_id': self.chat, 'text': message})

    def _photo(self, image: str, caption: str) -> bool:
        """Try sendPhoto; False (and logged) when the API refuses."""
        try:
            self.api.call(
                'sendPhoto',
                {'chat_id': self.chat, 'photo': image, 'caption': caption},
            )
        except TgError as exc:
            _LOG.warning('wishlist_photo_failed reason=%s', exc)
            return False
        return True
