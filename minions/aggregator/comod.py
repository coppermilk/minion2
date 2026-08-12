"""The cabinet ("komod"/"shkaf"): a shelf roster for supporters.

A supporter "moves into the cabinet" and gets a named shelf for a while; when
more than ``COMOD_TTL_SEC`` (seven days) pass since their move-in, they are
pruned -- the shelf frees up ("s'ekhal"). This is the same rolling-TTL shape as
the donations bot's bed roster, rebuilt here so the aggregator owns it.

The roster is a ``nick -> {at, amount}`` map on disk: ``at`` is the move-in
epoch (the seven-day timer), ``amount`` is how much they donated (a free
string, rendered as ``$N`` under the nick on the shelf). ``main.py`` drives it
from the ``/comod`` command and renders the current residents onto the cabinet
photo via ``minion_core.adapters.files.render_cabinet`` (the sole Pillow site).

All visible text (the move-in announcement, the roster header) lives in the
constants JSON, so this source stays ASCII.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

COMOD_TTL_SEC = 7 * 24 * 3600
"""How long a nick keeps its shelf before it frees up: seven days."""

# The prototype's shelf boxes for the 1080-wide portrait cabinet photo: the top
# full-width slot, three rows of paired cubbies, then three wide lower shelves.
# Each is (x, y, w, h). A different template photo needs different boxes.
_DEFAULT_SLOTS: tuple[tuple[int, int, int, int], ...] = (
    (347, 73, 412, 203),
    (340, 303, 199, 150),
    (560, 296, 198, 159),
    (341, 476, 198, 147),
    (558, 473, 198, 151),
    (342, 649, 193, 122),
    (556, 644, 179, 127),
    (346, 906, 406, 105),
    (346, 1050, 400, 93),
    (344, 1198, 391, 73),
)


@dataclass(frozen=True)
class ComodParams:
    """The cabinet's config, from the constants JSON 'comod' section."""

    templates: dict[str, object]
    donate_link: str
    template_path: str  # the base cabinet photo to draw onto
    font_path: str  # a Cyrillic-capable TTF (empty: fall back to system fonts)
    base_size: int
    slots: tuple[tuple[int, int, int, int], ...]
    max_shelves: int
    text_color: tuple[int, int, int]
    shadow_color: tuple[int, int, int]


@dataclass(frozen=True)
class CabinetRoster:
    """Who is in the cabinet: a nick kept for ``COMOD_TTL_SEC``.

    A ``nick -> {"at": epoch, "amount": str}`` map on disk. A fresh move-in
    refreshes the seven-day timer; expired nicks are pruned on every write, so
    the file never grows without bound. Methods take ``now`` so tests drive a
    clock (the same shape as the bed roster).
    """

    path: Path

    def _load(self) -> dict[str, dict[str, object]]:
        """Reload the roster, tolerating a missing or corrupt file."""
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, dict[str, object]] = {}
        for key, value in data.items():
            if isinstance(key, str) and isinstance(value, dict):
                out[key] = {
                    'at': float(value.get('at', 0.0) or 0.0),
                    'amount': str(value.get('amount', '')),
                }
        return out

    def _write(self, roster: dict[str, dict[str, object]]) -> None:
        """Persist the roster atomically as readable JSON."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(
            json.dumps(roster, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        tmp.replace(self.path)

    def add(self, nick: str, amount: str, now: float) -> None:
        """Move a nick into the cabinet, pruning anyone whose timer expired."""
        clean = nick.lstrip('@').strip()
        if not clean:
            return
        roster = self._load()
        roster[clean] = {'at': now, 'amount': amount.strip()}
        fresh = {
            n: e
            for n, e in roster.items()
            if now - float(e['at']) < COMOD_TTL_SEC
        }
        self._write(fresh)

    def remove(self, nick: str) -> bool:
        """Evict a nick by hand; True if it was there."""
        clean = nick.lstrip('@').strip()
        roster = self._load()
        if clean not in roster:
            return False
        del roster[clean]
        self._write(roster)
        return True

    def active(self, now: float) -> list[tuple[str, str]]:
        """(nick, amount) still in the cabinet, most recent move-in first."""
        roster = self._load()
        fresh = [
            (float(e['at']), n, str(e['amount']))
            for n, e in roster.items()
            if now - float(e['at']) < COMOD_TTL_SEC
        ]
        fresh.sort(key=lambda row: row[0], reverse=True)
        return [(nick, amount) for _, nick, amount in fresh]


def _color(
    value: object, default: tuple[int, int, int]
) -> tuple[int, int, int]:
    """Parse an [r, g, b] JSON list into an RGB tuple (default on garbage)."""
    if isinstance(value, (list, tuple)) and len(value) == 3:  # noqa: PLR2004
        try:
            r, g, b = (int(c) for c in value)
        except (TypeError, ValueError):
            return default
        return (r, g, b)
    return default


def _slots(value: object) -> tuple[tuple[int, int, int, int], ...]:
    """Parse a JSON list of [x, y, w, h] boxes; fall back to the defaults."""
    rows = value if isinstance(value, list) else []
    out: list[tuple[int, int, int, int]] = []
    for row in rows:
        if isinstance(row, (list, tuple)) and len(row) == 4:  # noqa: PLR2004
            try:
                x, y, w, h = (int(v) for v in row)
            except (TypeError, ValueError):
                continue
            out.append((x, y, w, h))
    return tuple(out) or _DEFAULT_SLOTS


def load_comod_params(data: dict[str, object]) -> ComodParams:
    """Load the cabinet's params from the constants JSON 'comod' section."""
    cfg = data.get('comod') if isinstance(data.get('comod'), dict) else {}
    cfg = cfg or {}
    templates = cfg.get('templates')
    templates = templates if isinstance(templates, dict) else {}
    render = cfg.get('render') if isinstance(cfg.get('render'), dict) else {}
    render = render or {}
    slots = _slots(render.get('slots'))
    return ComodParams(
        templates=templates,
        donate_link=str(cfg.get('donate_link', '')),
        template_path=str(render.get('template', '')),
        font_path=str(render.get('font', '')),
        base_size=int(render.get('base_size') or 40),
        slots=slots,
        max_shelves=int(render.get('max_shelves') or len(slots)),
        text_color=_color(render.get('text_color'), (255, 255, 255)),
        shadow_color=_color(render.get('shadow_color'), (0, 0, 0)),
    )


def _amount_label(amount: str) -> str:
    """A donated amount as ``$N`` (strips a leading currency the op typed)."""
    clean = amount.lstrip('$ ').strip()
    return f'${clean}' if clean else ''


def labels_for(residents: list[tuple[str, str]]) -> list[str]:
    r"""Each resident as a shelf label, nick over amount.

    The amount goes on its own line UNDER the nick (rendered centered), so a
    label is ``"nick\n$amount"`` -- or just the nick when no amount was given.
    """
    labels = []
    for nick, amount in residents:
        money = _amount_label(amount)
        labels.append(f'{nick}\n{money}' if money else nick)
    return labels


def _pick(value: object) -> str:
    """One string from a value that is either a list of variants or a str."""
    if isinstance(value, list):
        choices = [str(v) for v in value if isinstance(v, str)]
        return random.choice(choices) if choices else ''  # noqa: S311
    return str(value or '')


def move_in_text(templates: dict[str, object], nick: str, link: str) -> str:
    """The "you moved into the cabinet" announcement, {nick}/{link} filled."""
    body = _pick(templates.get('move_in'))
    return body.replace('{nick}', nick.lstrip('@').strip()).replace(
        '{link}', link
    )
