# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The formatting-span boundary, pinned on both sides of the door.

``premium_emoji`` builds spans in PYTHON CHARACTERS; the Telegram adapter
converts them to Telegram's UTF-16 code units. The two differ exactly on
non-BMP characters -- which is every emoji this bot sends -- so a shift of
one unit is not a rounding error, it is a message whose colored glyph lands
on the wrong letter. Both sides are asserted here against numbers captured
from the implementation that predates the split.
"""

from __future__ import annotations

from minion_core.adapters import userchat
from minion_core.richtext import EMOJI
from minion_core.richtext import LINK
from minion_core.richtext import UNDERLINE
from minion_core.richtext import Span
from minions.userbot.engines import premium_emoji
from tests.conftest import install_telethon_stub

install_telethon_stub()


FIRE = chr(0x1F525)  # non-BMP: one character, two UTF-16 units
HEART = chr(0x1F49B)  # non-BMP
CHECK = chr(0x2714)  # BMP: one character, one UTF-16 unit

CAT_ID = '5334681713316479679'
TG_ID = '5309876543210987654'
VK_ID = '5111111111111111111'

MARKUP = (
    f'{FIRE} start <tg-emoji emoji-id="{CAT_ID}">{HEART}'
    f'</tg-emoji> tail {CHECK}'
)
BODY = f'{FIRE} start {HEART} tail {CHECK}'

BAR = [
    premium_emoji.Social('tg', int(TG_ID), FIRE, 'https://t.me/x'),
    premium_emoji.Social('yt', None, CHECK, 'https://y.tv/z'),
    premium_emoji.Social('vk', int(VK_ID), HEART, ''),
]


def _triples(
    message: premium_emoji.PremiumMessage,
) -> list[tuple[object, ...]]:
    """Render the adapter's entities as (kind, offset, length, ref)."""
    out: list[tuple[object, ...]] = []
    for entity in userchat.entities(message.text, message.spans):
        kind = type(entity).__name__.removeprefix('MessageEntity')
        ref = getattr(entity, 'document_id', getattr(entity, 'url', None))
        out.append((kind, entity.offset, entity.length, ref))
    return out


def test_markup_spans_count_characters() -> None:
    """The engine side offsets in characters: the emoji sits at char 8."""
    message = premium_emoji.build_premium_message(MARKUP)
    assert message.text == BODY
    assert message.spans == (Span(EMOJI, 8, 1, CAT_ID),)


def test_markup_entities_count_utf16_units() -> None:
    """The adapter side offsets in UTF-16: the same emoji sits at unit 9."""
    message = premium_emoji.build_premium_message(MARKUP)
    assert _triples(message) == [('CustomEmoji', 9, 2, int(CAT_ID))]


def test_social_bar_spans() -> None:
    """A bar glyph carries its color and its tap target on one span."""
    message = premium_emoji.build_social_bar(BAR)
    assert message.text == f'{FIRE}   {CHECK}   {HEART}'
    assert message.spans == (
        Span(EMOJI, 0, 1, TG_ID),
        Span(LINK, 0, 1, 'https://t.me/x'),
        Span(LINK, 4, 1, 'https://y.tv/z'),
        Span(EMOJI, 8, 1, VK_ID),
    )


def test_social_bar_entities() -> None:
    """The same bar in Telegram's units, entry by entry."""
    message = premium_emoji.build_social_bar(BAR)
    assert _triples(message) == [
        ('CustomEmoji', 0, 2, int(TG_ID)),
        ('TextUrl', 0, 2, 'https://t.me/x'),
        ('TextUrl', 5, 1, 'https://y.tv/z'),
        ('CustomEmoji', 9, 2, int(VK_ID)),
    ]


def test_post_with_bar_shifts_the_footer() -> None:
    """The footer's spans move past the post body and the separator."""
    message = premium_emoji.build_post_with_bar(MARKUP, BAR)
    assert message.text == f'{BODY}\n\n{FIRE}   {CHECK}   {HEART}'
    assert _triples(message) == [
        ('CustomEmoji', 9, 2, int(CAT_ID)),
        ('CustomEmoji', 20, 2, int(TG_ID)),
        ('TextUrl', 20, 2, 'https://t.me/x'),
        ('TextUrl', 25, 1, 'https://y.tv/z'),
        ('CustomEmoji', 29, 2, int(VK_ID)),
    ]


def test_rich_text_builder() -> None:
    """A hand-assembled message: text, premium emoji, plain emoji, link."""
    message = (
        premium_emoji.RichText()
        .text(f'{FIRE}a ')
        .emoji({'id': CAT_ID, 'fallback': HEART})
        .emoji(CHECK)
        .link(f'{FIRE}link', 'https://example.org/')
        .text('!')
        .build()
    )
    assert message.text == f'{FIRE}a {HEART}{CHECK}{FIRE}link!'
    assert message.spans == (
        Span(EMOJI, 3, 1, CAT_ID),
        Span(LINK, 5, 5, 'https://example.org/'),
        Span(UNDERLINE, 5, 5, ''),
    )
    assert _triples(message) == [
        ('CustomEmoji', 4, 2, int(CAT_ID)),
        ('TextUrl', 7, 6, 'https://example.org/'),
        ('Underline', 7, 6, None),
    ]


def test_send_kwargs_choose_spans_or_html_but_not_both() -> None:
    """Premium spans and an HTML template are alternatives, not a pair."""
    spans = (Span(EMOJI, 0, 1, CAT_ID),)
    rich = userchat._send_kwargs(userchat.Text(HEART, spans=spans))
    assert 'formatting_entities' in rich
    assert 'parse_mode' not in rich
    markup = userchat._send_kwargs(userchat.Text(HEART, html=True))
    assert markup['parse_mode'] == 'html'
    assert 'formatting_entities' not in markup
