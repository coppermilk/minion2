"""Themeable progress rendering for the relay's one self-editing message.

The relay shows a task's whole life in a single message it edits; how that
message LOOKS is a pluggable style, so it can be reskinned -- blocks, emoji
squares, donuts, a growing plant, a spinner -- without touching the belt.

A style renders one line for a ``(phase, pct)``: ``downloading`` (a bar with
percent and a dopamine caption), ``sending`` (uploading the result),
``done``, ``error``. Adding a bar theme is one ``BarStyle`` data row; a
non-bar theme is a tiny class under the same Protocol.

Every glyph is built with ``chr(0x...)`` so the source stays ASCII
(BLUEPRINT), yet renders as the real symbol in Telegram.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

DOWNLOADING = 'downloading'
SENDING = 'sending'
DONE = 'done'
ERROR = 'error'

_CHEERS: Mapping[int, str] = {
    0: 'just started',
    25: 'warming up',
    50: 'halfway',
    80: 'almost there',
    100: 'done',
}
"""Dopamine caption shown at each milestone (the highest reached wins)."""


class ProgressStyle(Protocol):
    """One line of status for a phase and percent (0..100)."""

    def render(self, phase: str, pct: int) -> str:
        """The message text to show for ``phase`` at ``pct`` percent."""


def _clamp(pct: int) -> int:
    """Percent forced into 0..100 (an extractor may over/undershoot)."""
    return max(0, min(100, pct))


def _cheer(pct: int, cheers: Mapping[int, str]) -> str:
    """The dopamine caption for ``pct``: the highest milestone reached."""
    reached = [text for at, text in cheers.items() if pct >= at]
    return reached[-1] if reached else ''


@dataclass(frozen=True)
class BarStyle:
    """A bar theme: fill/empty glyphs, optional brackets, phase words.

    Data-driven, so a new theme is one row -- no code. ``empty=''`` gives a
    grow-only bar (e.g. donuts) whose length tracks the percent.
    """

    fill: str
    empty: str
    segments: int = 10
    left: str = ''
    right: str = ''
    sending: str = 'Sending...'
    done: str = 'Done'
    error: str = 'Error'
    cheers: Mapping[int, str] = field(default_factory=lambda: dict(_CHEERS))

    def render(self, phase: str, pct: int) -> str:
        """Render one status line for the phase (see module docstring)."""
        if phase == SENDING:
            return self.sending
        if phase == DONE:
            return self.done
        if phase == ERROR:
            return self.error
        return f'{self._bar(pct)} {_clamp(pct)}%  {_cheer(pct, self.cheers)}'

    def _bar(self, pct: int) -> str:
        """The bracketed fill/empty bar for ``pct``."""
        done = round(self.segments * _clamp(pct) / 100)
        body = self.fill * done + self.empty * (self.segments - done)
        return f'{self.left}{body}{self.right}'


@dataclass(frozen=True)
class StageStyle:
    """A non-bar theme: cumulative growth stages (a plant, phases of a moon).

    The stages fill up as the percent climbs, so 33% of three stages shows
    the first one -- proof the Protocol carries more than bars.
    """

    stages: str
    sending: str = 'Sending...'
    done: str = 'Done'
    error: str = 'Error'

    def render(self, phase: str, pct: int) -> str:
        """Render growth up to ``pct`` (see module docstring)."""
        if phase == SENDING:
            return self.sending
        if phase == DONE:
            return self.done
        if phase == ERROR:
            return self.error
        shown = 1 + _clamp(pct) * (len(self.stages) - 1) // 100
        return f'{self.stages[:shown]} {_clamp(pct)}%  growing'


@dataclass(frozen=True)
class SpinnerStyle:
    """A non-bar theme: a rotating spinner frame chosen by the percent."""

    frames: str
    label: str = 'downloading'
    sending: str = 'Sending...'
    done: str = 'Done'
    error: str = 'Error'

    def render(self, phase: str, pct: int) -> str:
        """Render a spinner frame + percent (see module docstring)."""
        if phase == SENDING:
            return self.sending
        if phase == DONE:
            return self.done
        if phase == ERROR:
            return self.error
        frame = self.frames[(_clamp(pct) // 5) % len(self.frames)]
        return f'{frame} {self.label} {_clamp(pct)}%'


# Glyphs as escapes only, so the source stays ASCII (it renders as symbols):
# rectangles, shades, squares, green/white squares, donut, plant, moons.
_BLOCK = (chr(0x25B0), chr(0x25B1))
_SHADE = (chr(0x2593), chr(0x2591))
_SQUARE = (chr(0x25A0), chr(0x25A1))
_GREEN = (chr(0x1F7E9), chr(0x2B1C))
_DONUT = chr(0x1F369)
_PLANT = chr(0x1F331) + chr(0x1F33F) + chr(0x1F333)
_MOONS = ''.join(map(chr, (0x25D0, 0x25D3, 0x25D1, 0x25D2)))
_CHECK = chr(0x2705)

STYLES: Mapping[str, ProgressStyle] = {
    'blocks': BarStyle(fill=_BLOCK[0], empty=_BLOCK[1]),
    'shade': BarStyle(fill=_SHADE[0], empty=_SHADE[1]),
    'squares': BarStyle(
        fill=_SQUARE[0], empty=_SQUARE[1], left='[', right=']'
    ),
    'emoji': BarStyle(fill=_GREEN[0], empty=_GREEN[1], segments=8),
    'donut': BarStyle(
        fill=_DONUT,
        empty='',
        segments=8,
        cheers={0: 'mmm', 50: 'tasty', 80: 'YUM', 100: 'YUM'},
        done=f'{_CHECK} done',
    ),
    'plant': StageStyle(stages=_PLANT),
    'spinner': SpinnerStyle(frames=_MOONS),
}
"""Every built-in theme by name; RELAY_PROGRESS_STYLE picks one."""

_DEFAULT = 'blocks'


def style_for(env: Mapping[str, str] | None = None) -> ProgressStyle:
    """The style named by RELAY_PROGRESS_STYLE, or the default block bar."""
    src = os.environ if env is None else env
    name = src.get('RELAY_PROGRESS_STYLE', _DEFAULT)
    chosen = STYLES.get(name)
    return chosen if chosen is not None else STYLES[_DEFAULT]


# Checklist glyphs: check, down-arrow, outbox tray, party, cross.
_DONE_G = chr(0x2705)
_DL_G = chr(0x2B07)
_UP_G = chr(0x1F4E4)
_PARTY_G = chr(0x1F389)
_FAIL_G = chr(0x274C)
_RECEIVED = f'{_DONE_G} Link received'


def checklist(style: ProgressStyle, phase: str, pct: int) -> str:
    """The growing checklist: done steps stay, the current one is live.

    The relay edits ONE message with this, so the user watches a list fill
    in -- received, downloading (a live bar), sending, done -- instead of a
    pile of messages. ``pct`` styles only the downloading line.
    """
    if phase == DOWNLOADING:
        return f'{_RECEIVED}\n{_DL_G} {style.render(DOWNLOADING, pct)}'
    downloaded = f'{_RECEIVED}\n{_DONE_G} Downloaded'
    if phase == SENDING:
        return f'{downloaded}\n{_UP_G} Sending...'
    return f'{downloaded}\n{_DONE_G} Sent\n{_PARTY_G} Done'


def checklist_error(detail: str) -> str:
    """The checklist with the current step failed -- received, then why."""
    return f'{_RECEIVED}\n{_FAIL_G} {detail}'
