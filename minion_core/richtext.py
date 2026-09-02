# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Formatting spans over a message, measured in Python characters.

A span says "this run of the text is a premium emoji / a link / underlined"
and points at whatever the run needs: an emoji id, a URL. The offset counts
Python CHARACTERS -- the unit the code assembling a message can actually
see -- and each platform adapter converts to the unit that platform
measures in. Telegram counts UTF-16 code units, where an emoji is one
character but two units; another platform will count something else.

Keeping that conversion behind the adapter is what lets a message builder
stay platform-free: ``engines/premium_emoji`` composes spans and never
learns which service the text is bound for.
"""

from __future__ import annotations

from dataclasses import dataclass

EMOJI = 'emoji'
LINK = 'link'
UNDERLINE = 'underline'
"""The kinds of run a message can carry."""


@dataclass(frozen=True)
class Span:
    """One formatting run: ``length`` characters starting at ``at``.

    ``ref`` is what the run points at -- a custom-emoji id as its decimal
    string for ``EMOJI``, a URL for ``LINK``, nothing for a plain style.
    Spans may overlap: a colored glyph that is also tappable is one
    ``EMOJI`` and one ``LINK`` over the same characters.
    """

    kind: str
    at: int
    length: int
    ref: str = ''
