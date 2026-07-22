"""Parse Bot-API-style ``<tg-emoji>`` markup into Telethon custom-emoji entities.

Telegram premium (custom) emoji travel over MTProto as
``MessageEntityCustomEmoji`` entities that point a stretch of the message
text at a ``document_id`` (the emoji id).  Telethon's built-in HTML parser
does not understand the ``<tg-emoji emoji-id="...">X</tg-emoji>`` tag that the
Bot API uses, so we translate it ourselves.

Two rules matter and are easy to get wrong:

* Telegram measures entity ``offset``/``length`` in **UTF-16 code units**, not
  Python characters.  A single emoji such as 📱 is one Python character but two
  UTF-16 units, so we count in UTF-16 here.
* The placeholder character kept in the text (the 📱 between the tags) is what a
  non-premium client shows when it cannot render the custom emoji.  It should be
  a sensible fallback glyph.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from telethon.tl.types import MessageEntityCustomEmoji

# <tg-emoji emoji-id="5334681713316479679">📱</tg-emoji>
_TG_EMOJI_RE = re.compile(
    r'<tg-emoji\s+emoji-id="(?P<id>\d+)"\s*>(?P<fallback>.*?)</tg-emoji>',
    re.DOTALL,
)


@dataclass(frozen=True)
class PremiumMessage:
    """A plain-text message plus the custom-emoji entities that decorate it."""

    text: str
    entities: list[MessageEntityCustomEmoji]


def _utf16_len(text: str) -> int:
    """Length of ``text`` in UTF-16 code units — Telegram's entity unit."""
    return len(text.encode("utf-16-le")) // 2


def build_premium_message(markup: str) -> PremiumMessage:
    """Turn ``<tg-emoji>`` markup into text + ``MessageEntityCustomEmoji`` list.

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

        fallback = match.group("fallback")
        length = _utf16_len(fallback)
        entities.append(
            MessageEntityCustomEmoji(
                offset=offset,
                length=length,
                document_id=int(match.group("id")),
            )
        )
        text_parts.append(fallback)
        offset += length
        cursor = match.end()

    text_parts.append(markup[cursor:])
    return PremiumMessage(text="".join(text_parts), entities=entities)
