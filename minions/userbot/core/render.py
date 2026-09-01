# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Compose the published post and the /emojis catalog message.

Extracted from ``main``: everything that turns a Group (plus the loaded
Consts) into a premium-emoji message -- the author/description lines and the
aligned platform-link grid -- plus the QC sample builders. Pure functions
over the models and ``premium_emoji``; no client, no state.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

from minions.userbot.core.humanize import Variety
from minions.userbot.core.matching import HASHTAG_RE
from minions.userbot.core.matching import primary
from minions.userbot.core.models import Emoji
from minions.userbot.core.models import Group
from minions.userbot.core.models import Item
from minions.userbot.engines.premium_emoji import RichText

if TYPE_CHECKING:
    from collections.abc import Iterable

    from minions.userbot.core.models import Consts
    from minions.userbot.engines.premium_emoji import PremiumMessage

# The order emoji types are grouped in for the /emojis catalog message.
_EMOJI_ORDER = ('love', 'lead', 'arrow', 'platform', 'reaction', 'like')


@dataclass(frozen=True)
class Glyphs:
    """The two /status glyphs a service needs to render its own rows.

    The report's icons live in the constants JSON, so a service that renders
    a row of it (the queued reactions) is handed just these rather than the
    whole Consts -- it has no other business with the report's wording.
    """

    bullet: str = '-'
    arrow: str = '->'


def trim(title: str, width: int = 40) -> str:
    """Return a one-line, length-capped title for a report row."""
    flat = ' '.join(title.split())
    return flat if len(flat) <= width else flat[: width - 1] + '~'


def youtube_thumb(group: Group) -> str:
    """Return the thumbnail URL from the YouTube item only, or ''."""
    item = group.items.get('youtube')
    return item.thumbnail if item else ''


def _strip_tags(caption: str) -> str:
    """Caption without its trailing hashtags, for display."""
    return ' '.join(HASHTAG_RE.sub(' ', caption).split())


def emoji_markup(emoji_id: str, fallback: str) -> str:
    """One `<tg-emoji>` tag so /status renders the real premium emoji.

    ``build_premium_message`` turns this into the custom-emoji entity (the
    fallback glyph shows for non-premium viewers).
    """
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def _cells(group: Group, row: list[str]) -> list[str]:
    """Platform keys in a row that have a link, in the row's order."""
    return [p for p in row if group.items.get(p) and group.items[p].url]


def _grid_cells(group: Group, consts: Consts) -> list[list[tuple[str, str]]]:
    """Pre-pick each cell's (platform key, random label), row by row."""
    return [
        [
            (key, random.choice(consts.view_label))  # noqa: S311
            for key in _cells(group, row)
        ]
        for row in consts.rows
    ]


def _col_widths(rows: list[list[tuple[str, str]]]) -> dict[int, int]:
    """Return the longest label used in each column (to align '|')."""
    widths: dict[int, int] = {}
    for row in rows:
        for col, (_key, label) in enumerate(row):
            widths[col] = max(widths.get(col, 0), len(label))
    return widths


def _compose_links(rich: RichText, group: Group, consts: Consts) -> None:
    """Append the platform link grid: '<emoji> View | <emoji> View' rows.

    Each label is padded only to the longest label actually used in its
    column, so the '|' separators line up with no wasted space -- and the last
    cell of a row is never padded (no trailing spaces).
    """
    rows = _grid_cells(group, consts)
    widths = _col_widths(rows)
    for row in rows:
        last = len(row) - 1
        for index, (key, label) in enumerate(row):
            rich.emoji(consts.platform_emoji.get(key, Emoji())).text(' ')
            rich.link(label, group.items[key].url)
            if index != last:
                pad = ' ' * (widths[index] - len(label))
                rich.text(pad + consts.column_separator)
        if row:
            rich.text('\n')


def _catalog_suffix(entry: Emoji) -> str:
    """Return the trailing id + platform / reaction tags for one entry."""
    parts = [entry.id or '?']
    if entry.name:
        parts.append(entry.name)
    if entry.tags:
        parts.append('[' + ','.join(entry.tags) + ']')
    return ' '.join(parts)


def render_constants(consts: Consts) -> PremiumMessage:
    """Render the whole unified emoji array for /emojis, grouped by type."""
    rich = RichText()
    rich.text(f'Premium emoji ({len(consts.emoji_all)})\n\n')
    by_type: dict[str, list[Emoji]] = {}
    for entry in consts.emoji_all:
        by_type.setdefault(entry.kind or 'other', []).append(entry)
    extra = [t for t in by_type if t not in _EMOJI_ORDER]
    for etype in (*_EMOJI_ORDER, *extra):
        items = by_type.get(etype)
        if not items:
            continue
        rich.text(f'{etype} ({len(items)}):\n')
        for entry in items:
            rich.emoji(entry).text(' ' + _catalog_suffix(entry) + '\n')
        rich.text('\n')
    return rich.build()


def compose(  # noqa: PLR0913 -- variety is an optional post-decoration picker
    group: Group,
    order: tuple[str, ...],
    consts: Consts,
    variety: Variety | None = None,
) -> PremiumMessage:
    """Build the full post: author line, description line, and link grid.

    ``variety`` picks the announce line and the love/lead/arrow emoji so a
    post does not repeat what the previous one used (see ``humanize``).
    Default is a fresh, memory-less picker -- the real post path passes a
    persistent one; previews and tests get plain, independent variety.
    """
    pick = (variety or Variety()).pick
    caption = _strip_tags(primary(group, order).title)
    rich = RichText()
    rich.text(consts.author).text(' ')
    rich.text(pick('announce', consts.announce)).text(' ')
    rich.emoji(pick('love', consts.love)).text('\n\n')
    rich.emoji(pick('lead', consts.lead)).text(' ')
    rich.text(caption).text(' ')
    rich.emoji(pick('arrow', consts.arrow_down)).text('\n\n')
    _compose_links(rich, group, consts)
    return rich.build()


# QC preview (/preview): fake titles + dummy links, one video per scenario.
# The two sample captions live in the constants JSON (so this source stays
# ASCII, BLUEPRINT 4); sample_groups reads them off the loaded Consts.


def _sample_item(key: str, msg_id: int, title: str) -> Item:
    """One fake platform item for a QC preview post."""
    thumb = 'https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg'
    return Item(
        key=key,
        platform=key,
        title=title,
        url=f'https://example.com/{key}',
        thumbnail=thumb if key == 'youtube' else '',
        duration='0:0:20',
        msg_id=msg_id,
    )


def _sample_group(platforms: Iterable[str], title: str) -> Group:
    """Return a fake Group covering the given platforms, for a QC preview."""
    group = Group(title=title)
    for msg_id, key in enumerate(platforms):
        group.items[key] = _sample_item(key, msg_id, title)
    return group


def sample_groups(consts: Consts) -> list[Group]:
    """Five sample videos for QC: from one platform arrived up to all four."""
    short = consts.sample_short
    long = consts.sample_long
    return [
        _sample_group(['youtube'], short),
        _sample_group(['tiktok', 'youtube'], short),
        _sample_group(['instagram', 'pinterest'], short),
        _sample_group(['tiktok', 'youtube', 'instagram'], long),
        _sample_group(['tiktok', 'youtube', 'pinterest', 'instagram'], short),
    ]
