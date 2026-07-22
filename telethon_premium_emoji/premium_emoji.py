"""Parse Bot-API ``<tg-emoji>`` markup into Telethon custom-emoji entities.

Telegram premium (custom) emoji travel over MTProto as
``MessageEntityCustomEmoji`` entities that point a stretch of the message
text at a ``document_id`` (the emoji id).  Telethon's built-in HTML parser
does not understand the ``<tg-emoji emoji-id="...">X</tg-emoji>`` tag that
the Bot API uses, so we translate it ourselves.

Two rules matter and are easy to get wrong:

* Telegram measures entity ``offset``/``length`` in **UTF-16 code units**,
  not Python characters.  A single emoji is one Python character but two
  UTF-16 units, so we count in UTF-16 here.
* The placeholder character kept in the text (the fallback glyph between
  the tags) is what a non-premium client shows when it cannot render the
  custom emoji.  It should be a sensible fallback glyph.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from telethon.tl.types import MessageEntityCustomEmoji
from telethon.tl.types import MessageEntityTextUrl

if TYPE_CHECKING:
    from collections.abc import Sequence

    from telethon.tl.types import TypeMessageEntity

# <tg-emoji emoji-id="5334681713316479679">X</tg-emoji>
_TG_EMOJI_RE = re.compile(
    r'<tg-emoji\s+emoji-id="(?P<id>\d+)"\s*>(?P<fallback>.*?)</tg-emoji>',
    re.DOTALL,
)


@dataclass(frozen=True)
class PremiumMessage:
    """A plain-text message plus the entities that decorate it."""

    text: str
    entities: list[TypeMessageEntity] = field(default_factory=list)


@dataclass(frozen=True)
class Social:
    """One social-bar entry: a colored glyph that links to a platform.

    ``emoji_id`` is a premium custom-emoji document id -- the colored
    platform logo. When it is ``None`` the plain ``fallback`` glyph shows
    instead, so the bar still renders before you have all the ids. A
    non-empty ``url`` makes the glyph tappable.
    """

    name: str
    emoji_id: int | None
    fallback: str
    url: str = ''


def _utf16_len(text: str) -> int:
    """Length of ``text`` in UTF-16 code units -- Telegram's entity unit."""
    return len(text.encode('utf-16-le')) // 2


def build_premium_message(markup: str) -> PremiumMessage:
    """Turn ``<tg-emoji>`` markup into text + custom-emoji entities.

    Text outside the tags is passed through untouched.  Each tag contributes
    one entity whose fallback glyph stays in the visible text.
    """
    text_parts: list[str] = []
    entities: list[MessageEntityCustomEmoji] = []
    offset = 0  # running position in UTF-16 code units
    cursor = 0  # position in the source markup

    for match in _TG_EMOJI_RE.finditer(markup):
        before = markup[cursor : match.start()]
        text_parts.append(before)
        offset += _utf16_len(before)

        fallback = match.group('fallback')
        length = _utf16_len(fallback)
        entities.append(
            MessageEntityCustomEmoji(
                offset=offset,
                length=length,
                document_id=int(match.group('id')),
            )
        )
        text_parts.append(fallback)
        offset += length
        cursor = match.end()

    text_parts.append(markup[cursor:])
    return PremiumMessage(text=''.join(text_parts), entities=entities)


def build_social_bar(
    entries: Sequence[Social],
    *,
    separator: str = '   ',
) -> PremiumMessage:
    """Render a row of colored, tappable premium-emoji "buttons".

    A Telegram user account cannot send real inline buttons (bot-only), and
    button labels never render premium emoji -- so the closest thing is a
    line of premium emoji, each linked to its platform. Every glyph carries
    a custom-emoji entity (its color) and, when a url is set, an overlapping
    text-url entity on the same span (its tap target).
    """
    text_parts: list[str] = []
    entities: list[TypeMessageEntity] = []
    offset = 0

    for index, entry in enumerate(entries):
        if index:  # a separator sits between glyphs, not before the first
            offset += _utf16_len(separator)
            text_parts.append(separator)
        glyph = entry.fallback
        length = _utf16_len(glyph)
        if entry.emoji_id is not None:
            entities.append(
                MessageEntityCustomEmoji(offset, length, entry.emoji_id)
            )
        if entry.url:
            entities.append(MessageEntityTextUrl(offset, length, entry.url))
        text_parts.append(glyph)
        offset += length

    return PremiumMessage(text=''.join(text_parts), entities=entities)
