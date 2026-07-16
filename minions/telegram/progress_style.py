"""Progress rendering for the relay's one self-editing status message.

The relay edits ONE message through a task's life; each state is a compact
block -- a phase label, a fixed-width bar, and a detail line:

    link received
    ------------------  0%

    downloading
    ########----------  52%
    24.8 / 47.6 MB . 12s left

    sending
    ##################  100%

    done
    ##################  47.6 MB . 47s

The bar glyphs are themeable (``RELAY_PROGRESS_STYLE``); the layout is fixed.
Every glyph is built with ``chr(0x...)`` so the source stays ASCII (BLUEPRINT)
yet renders as the real symbol in Telegram. The byte/ETA detail lines are
formatted in ``minion_core.progress`` (shared with the fetch adapter).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from minion_core import progress

if TYPE_CHECKING:
    from collections.abc import Mapping

RECEIVED = 'received'
DOWNLOADING = 'downloading'
SENDING = 'sending'
DONE = 'done'
ERROR = 'error'

_LABELS: Mapping[str, str] = {
    RECEIVED: 'link received',
    DOWNLOADING: 'downloading',
    SENDING: 'sending',
    DONE: 'done',
    ERROR: 'error',
}

_BAR_WIDTH = 18


@dataclass(frozen=True)
class Style:
    """A bar theme: the filled and empty glyphs and the bar width.

    ``render`` lays out one status block for a phase, its progress Report,
    and (for done/error) a detail line the caller supplies.
    """

    fill: str
    empty: str
    width: int = _BAR_WIDTH

    def render(
        self, phase: str, report: progress.Report, detail: str = ''
    ) -> str:
        """One status block: label, bar, and the phase's detail line."""
        label = _LABELS.get(phase, phase)
        if phase == ERROR:
            return f'{label}\n{detail}'
        bar = self._bar(report.pct)
        if phase == DONE:
            return f'{label}\n{bar}  {detail}' if detail else f'{label}\n{bar}'
        head = f'{label}\n{bar}  {report.pct}%'
        sub = (
            progress.downloading_detail(report) if phase == DOWNLOADING else ''
        )
        return f'{head}\n{sub}' if sub else head

    def _bar(self, pct: int) -> str:
        """The fixed-width filled/empty bar for ``pct``."""
        filled = round(self.width * max(0, min(100, pct)) / 100)
        return self.fill * filled + self.empty * (self.width - filled)


# Glyph pairs as escapes, so the source stays ASCII (renders as symbols):
# full block / light horizontal, dark / light shade, filled / empty square.
STYLES: Mapping[str, Style] = {
    'blocks': Style(chr(0x2588), chr(0x2500)),
    'shade': Style(chr(0x2593), chr(0x2591)),
    'squares': Style(chr(0x25A0), chr(0x25A1)),
    'dots': Style(chr(0x2588), chr(0x2591)),
}
"""Every built-in bar theme by name; RELAY_PROGRESS_STYLE picks one."""

_DEFAULT = 'blocks'


def style_for(env: Mapping[str, str] | None = None) -> Style:
    """The style named by RELAY_PROGRESS_STYLE, or the default block bar."""
    src = os.environ if env is None else env
    name = src.get('RELAY_PROGRESS_STYLE', _DEFAULT)
    return STYLES.get(name, STYLES[_DEFAULT])
