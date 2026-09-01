# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Compose messages that carry premium emoji, links and underlines.

Telegram premium (custom) emoji travel as an entity that points a stretch
of the message text at an emoji id. Telethon's HTML parser does not
understand the ``<tg-emoji emoji-id="...">X</tg-emoji>`` tag the Bot API
uses, so we translate it ourselves -- into neutral ``richtext.Span`` runs,
not into Telegram entities. The adapter turns spans into whatever the
transport wants at send time, so nothing here knows about Telegram.

Two rules matter and are easy to get wrong:

* Offsets here count **Python characters**. Telegram measures entities in
  UTF-16 code units, where an emoji is two -- that conversion belongs to
  the adapter, and a message assembled here would be silently misaligned
  if it tried to guess.
* The placeholder character kept in the text (the fallback glyph between
  the tags) is what a non-premium client shows when it cannot render the
  custom emoji. It should be a sensible fallback glyph.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from minion_core.richtext import EMOJI
from minion_core.richtext import LINK
from minion_core.richtext import UNDERLINE
from minion_core.richtext import Span

if TYPE_CHECKING:
    from minions.userbot.core.models import Emoji

# <tg-emoji emoji-id="5334681713316479679">X</tg-emoji>
_TG_EMOJI_RE = re.compile(
    r'<tg-emoji\s+emoji-id="(?P<id>\d+)"\s*>(?P<fallback>.*?)</tg-emoji>',
    re.DOTALL,
)


@dataclass(frozen=True)
class PremiumMessage:
    """A plain-text message plus the spans that decorate it."""

    text: str
    spans: tuple[Span, ...] = ()


def build_premium_message(markup: str) -> PremiumMessage:
    """Turn ``<tg-emoji>`` markup into text plus custom-emoji spans.

    Text outside the tags is passed through untouched. Each tag contributes
    one span whose fallback glyph stays in the visible text.
    """
    parts: list[str] = []
    spans: list[Span] = []
    at = 0  # running position in the assembled text
    cursor = 0  # position in the source markup

    for match in _TG_EMOJI_RE.finditer(markup):
        before = markup[cursor : match.start()]
        parts.append(before)
        at += len(before)

        fallback = match.group('fallback')
        spans.append(Span(EMOJI, at, len(fallback), match.group('id')))
        parts.append(fallback)
        at += len(fallback)
        cursor = match.end()

    parts.append(markup[cursor:])
    return PremiumMessage(''.join(parts), tuple(spans))


class RichText:
    """Assemble a message from text, premium emoji, and underlined links.

    Offsets are tracked in Python characters (see the module docstring).
    Each method returns ``self`` for chaining; call ``build()`` for the
    final message.
    """

    def __init__(self) -> None:
        """Start an empty premium-emoji message builder."""
        self._parts: list[str] = []
        self._spans: list[Span] = []
        self._at = 0

    def text(self, value: str) -> RichText:
        """Append plain text."""
        self._parts.append(value)
        self._at += len(value)
        return self

    def emoji(self, emoji: Emoji) -> RichText:
        """Append one catalog emoji: premium when it has an id, else plain.

        An entry with no id (including the empty one a missing catalog
        role falls back to) contributes only its glyph, so an unconfigured
        decoration renders as nothing rather than as a hole.
        """
        if not emoji.id:
            return self.text(emoji.fallback)
        glyph = emoji.fallback or ' '
        self._spans.append(Span(EMOJI, self._at, len(glyph), emoji.id))
        return self.text(glyph)

    def link(self, label: str, url: str) -> RichText:
        """Append an underlined text link."""
        size = len(label)
        if url:
            self._spans.append(Span(LINK, self._at, size, url))
        self._spans.append(Span(UNDERLINE, self._at, size))
        return self.text(label)

    def build(self) -> PremiumMessage:
        """Return the assembled text and its spans."""
        return PremiumMessage(''.join(self._parts), tuple(self._spans))
