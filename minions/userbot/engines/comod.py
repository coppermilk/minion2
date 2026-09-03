# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The cabinet ("komod"/"shkaf"): a shelf roster for supporters.

A supporter "moves into the cabinet" and gets a named shelf for a while; when
more than ``COMOD_TTL_SEC`` (a month) passes since their move-in, they are
pruned -- the shelf frees up ("s'ekhal"). This is the same rolling-TTL shape as
the donations bot's bed roster, rebuilt here so the aggregator owns it.

The roster is a ``nick -> {at, amount}`` map on disk: ``at`` is the move-in
epoch (the month-long timer), ``amount`` is how much they donated (a free
string, rendered as ``$N`` under the nick on the shelf). ``main.py`` drives it
from the ``/comod`` command and renders the current residents onto the cabinet
photo via ``minion_core.adapters.files.render_cabinet`` (the sole Pillow site).

All visible text (the move-in announcement, the roster header) lives in the
constants JSON, so this source stays ASCII.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from minions.userbot.core import codec
from minions.userbot.core import state

COMOD_TTL_SEC = 30 * 24 * 3600
"""How long a nick keeps its shelf before it frees up: a month (30 days)."""

# A parsed colour row is [r, g, b]; a slot box is [x, y, w, h].
_RGB_LEN = 3
_SLOT_LEN = 4

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
    donate_link: str  # the {link} in the announcement (the donation URL)
    amazon_link: str  # the {amazon} in the announcement (the wishlist URL)
    template_path: str  # the base cabinet photo to draw onto
    font_path: str  # the primary TTF (empty: fall back to system fonts)
    font_cyrillic_path: str  # fallback TTF for lines with Cyrillic (may be '')
    base_size: int
    amount_scale: float  # the amount font relative to the fitted nick font
    ref_size: tuple[int, int]  # (w, h) the slot coords were measured against
    slots: tuple[tuple[int, int, int, int], ...]
    max_shelves: int
    text_color: tuple[int, int, int]
    shadow_color: tuple[int, int, int]
    # Premium hearts for the /propiska list: (emoji_id, fallback) pairs, one
    # picked at random per line. A blank id means a plain (non-premium) glyph.
    hearts: tuple[tuple[str, str], ...]
    tz_offset: float  # persona UTC offset, for the move-in date on /propiska


@dataclass(frozen=True)
class CabinetRoster:
    """Who is in the cabinet: a nick kept for ``COMOD_TTL_SEC``.

    A ``nick -> {"at": epoch, "amount": str}`` map on disk. A fresh move-in
    refreshes the month-long timer; expired nicks are pruned on every write, so
    the file never grows without bound. Methods take ``now`` so tests drive a
    clock (the same shape as the bed roster).
    """

    store: state.StateStore

    def add(self, nick: str, amount: str, now: float) -> None:
        """Move a nick into the cabinet, pruning anyone whose timer expired."""
        clean = nick.lstrip('@').strip()
        if not clean:
            return
        self.store.shelve(clean, now, amount.strip())
        self.store.sweep_cabinet(now - COMOD_TTL_SEC)

    def remove(self, nick: str) -> bool:
        """Evict a nick by hand; True if it was there."""
        return self.store.evict(nick.lstrip('@').strip())

    def active(self, now: float) -> list[tuple[str, str]]:
        """(nick, amount) still in the cabinet, most recent move-in first."""
        return [(nick, amount) for nick, amount, _ in self.entries(now)]

    def entries(self, now: float) -> list[tuple[str, str, float]]:
        """(nick, amount, move-in epoch), most recent move-in first.

        The dated view behind the month's registry (/propiska): the same
        fresh residents as ``active`` but carrying their move-in time.
        Expiry is a WHERE clause, so reading no longer evicts anybody as a
        side effect of being read.
        """
        return [
            (str(r['nick']), str(r['amount']), float(r['at']))
            for r in self.store.residents(now - COMOD_TTL_SEC)
        ]


def _color(
    value: object, default: tuple[int, int, int]
) -> tuple[int, int, int]:
    """Parse an [r, g, b] JSON list into an RGB tuple (default on garbage)."""
    if isinstance(value, (list, tuple)) and len(value) == _RGB_LEN:
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
        if isinstance(row, (list, tuple)) and len(row) == _SLOT_LEN:
            try:
                x, y, w, h = (int(v) for v in row)
            except (TypeError, ValueError):
                continue
            out.append((x, y, w, h))
    return tuple(out) or _DEFAULT_SLOTS


def _hearts(value: object) -> tuple[tuple[str, str], ...]:
    """Parse a JSON list of {id, fallback} hearts into (id, fb) pairs."""
    rows = value if isinstance(value, list) else []
    return tuple(
        (str(row.get('id', '')), str(row.get('fallback', '')))
        for row in rows
        if isinstance(row, dict)
    )


def _tz_offset(data: dict[str, object]) -> float:
    """Return the persona's UTC offset, as the persona fan left it here."""
    try:
        return float(
            codec.engine(data, 'comod').get('tz_offset_hours', 3.0)  # type: ignore[arg-type]
        )
    except (TypeError, ValueError):
        return 3.0


def load_comod_params(data: dict[str, object]) -> ComodParams:
    """Load the cabinet's params from the constants JSON 'comod' section."""
    cfg = codec.engine(data, 'comod')
    render = codec.table(cfg.get('render'))
    slots = _slots(render.get('slots'))
    return ComodParams(
        templates={
            k: str(v) for k, v in codec.table(cfg.get('templates')).items()
        },
        donate_link=codec.text(cfg.get('donate_link')),
        amazon_link=codec.text(cfg.get('amazon_link')),
        template_path=codec.text(render.get('template')),
        font_path=codec.text(render.get('font')),
        font_cyrillic_path=codec.text(render.get('font_cyrillic')),
        base_size=codec.whole(render.get('base_size')) or 40,
        amount_scale=codec.num(render.get('amount_scale')) or 0.75,
        ref_size=(
            codec.whole(render.get('ref_width')) or 1080,
            codec.whole(render.get('ref_height')) or 1350,
        ),
        slots=slots,
        max_shelves=codec.whole(render.get('max_shelves')) or len(slots),
        text_color=_color(render.get('text_color'), (255, 255, 255)),
        shadow_color=_color(render.get('shadow_color'), (0, 0, 0)),
        hearts=_hearts(cfg.get('hearts')),
        tz_offset=_tz_offset(data),
    )


def _amount_label(amount: str) -> str:
    """Return a donated amount as ``$N`` (strips a leading currency)."""
    clean = amount.lstrip('$ ').strip()
    return f'${clean}' if clean else ''


def _amount_value(amount: str) -> float:
    """Return the numeric part of an amount, for ranking (0.0 if none)."""
    digits = ''.join(c for c in amount if c.isdigit() or c == '.')
    try:
        return float(digits)
    except ValueError:
        return 0.0


def _label(nick: str, amount: str) -> str:
    """One shelf label: the nick, with its amount stacked underneath."""
    money = _amount_label(amount)
    return f'{nick}\n{money}' if money else nick


def by_amount(residents: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Residents sorted by donated amount, biggest first (stable on ties)."""
    return sorted(residents, key=lambda r: _amount_value(r[1]), reverse=True)


def assign_labels(
    residents: list[tuple[str, str]],
    slots: tuple[tuple[int, int, int, int], ...],
) -> list[str]:
    """Shelf labels aligned to ``slots``, biggest donor first, top to bottom.

    Residents are ranked by donated amount and dropped onto the slots in their
    listed order (which runs top shelf downward), so the biggest donor takes
    the top shelf and the rest follow down the cabinet. The result is in SLOT
    order (``label[i]`` belongs to ``slots[i]``); slots past the resident count
    stay blank.
    """
    labels = [''] * len(slots)
    for i, (nick, amount) in enumerate(by_amount(residents)):
        if i >= len(slots):
            break
        labels[i] = _label(nick, amount)
    return labels


def _pick(value: object) -> str:
    """One string from a value that is either a list of variants or a str."""
    if isinstance(value, list):
        choices = [str(v) for v in value if isinstance(v, str)]
        return random.choice(choices) if choices else ''  # noqa: S311
    return str(value or '')


def move_in_text(
    templates: dict[str, object], nick: str, links: dict[str, str]
) -> str:
    """Return the "you moved in" announcement, {nick} and link URLs filled.

    ``links`` maps a placeholder name to a URL, so ``{'link': ..., 'amazon':
    ...}`` fills ``{link}`` and ``{amazon}`` in the template.
    """
    body = _pick(templates.get('move_in')).replace(
        '{nick}', nick.lstrip('@').strip()
    )
    for name, url in links.items():
        body = body.replace('{' + name + '}', url)
    return body
