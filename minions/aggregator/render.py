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
from typing import TYPE_CHECKING

from minions.aggregator.matching import _HASHTAG_RE
from minions.aggregator.matching import _primary
from minions.aggregator.models import Group
from minions.aggregator.models import Item
from minions.aggregator.premium_emoji import RichText

if TYPE_CHECKING:
    from collections.abc import Iterable

    from minions.aggregator.models import Consts
    from minions.aggregator.premium_emoji import PremiumMessage

# The order emoji types are grouped in for the /emojis catalog message.
_EMOJI_ORDER = ('love', 'lead', 'arrow', 'platform', 'cat', 'like')


def _youtube_thumb(group: Group) -> str:
    """Return the thumbnail URL from the YouTube item only, or ''."""
    item = group.items.get('youtube')
    return item.thumbnail if item else ''


def _strip_tags(caption: str) -> str:
    """Caption without its trailing hashtags, for display."""
    return ' '.join(_HASHTAG_RE.sub(' ', caption).split())


def _emoji_markup(emoji_id: str, fallback: str) -> str:
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
            rich.emoji(consts.platform_emoji.get(key, '')).text(' ')
            rich.link(label, group.items[key].url)
            if index != last:
                pad = ' ' * (widths[index] - len(label))
                rich.text(pad + consts.column_separator)
        if row:
            rich.text('\n')


def _catalog_suffix(entry: dict[str, object]) -> str:
    """Return the trailing id + platform / cat tags for one entry."""
    parts = [str(entry.get('id', '?'))]
    if entry.get('name'):
        parts.append(str(entry['name']))
    tags = entry.get('tags')
    if tags:
        parts.append('[' + ','.join(str(t) for t in tags) + ']')
    return ' '.join(parts)


def _render_constants(consts: Consts) -> PremiumMessage:
    """Render the whole unified emoji array for /emojis, grouped by type."""
    rich = RichText()
    rich.text(f'Premium emoji ({len(consts.emoji_all)})\n\n')
    by_type: dict[str, list[dict[str, object]]] = {}
    for entry in consts.emoji_all:
        by_type.setdefault(str(entry.get('type', 'other')), []).append(entry)
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


def _compose(
    group: Group, order: tuple[str, ...], consts: Consts
) -> PremiumMessage:
    """Build the full post: author line, description line, and link grid."""
    caption = _strip_tags(_primary(group, order).title)
    rich = RichText()
    rich.text(consts.author).text(' ')
    rich.text(random.choice(consts.announce)).text(' ')  # noqa: S311
    rich.emoji(random.choice(consts.love)).text('\n\n')  # noqa: S311
    rich.emoji(random.choice(consts.lead)).text(' ')  # noqa: S311
    rich.text(caption).text(' ')
    rich.emoji(random.choice(consts.arrow_down)).text('\n\n')  # noqa: S311
    _compose_links(rich, group, consts)
    return rich.build()


# QC preview (/preview): fake titles + dummy links, one video per scenario.
# The two sample captions live in the constants JSON (so this source stays
# ASCII, BLUEPRINT 4); _sample_groups reads them off the loaded Consts.


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


def _sample_groups(consts: Consts) -> list[Group]:
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
