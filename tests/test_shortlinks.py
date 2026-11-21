# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The post's video links come out as the site shortener's addresses.

The expected codes below are real rows of the shortener's database
(``/admin/links.json`` of daetotyoma.com, 2026-11-21), so the test pins the
formula to what the worker actually serves -- not to itself.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from minion_core.richtext import LINK
from minions.userbot.core import config
from minions.userbot.core import render
from minions.userbot.core import shortlinks
from minions.userbot.core.models import Group
from minions.userbot.core.models import Item

DOMAIN = 'daetotyoma.com'

# (URL as the networks hand it out, the worker's key, the worker's code)
KNOWN = [
    (
        'https://www.tiktok.com/@da_eto_tyoma/video/7655351786026306838',
        'tiktok:7655351786026306838',
        'ey8vhjp',
    ),
    (
        'https://www.youtube.com/watch?v=BwCeG2Qh1Ag',
        'youtube:BwCeG2Qh1Ag',
        '5c2xd36',
    ),
    (
        'https://www.instagram.com/reel/DbI2s59EUda/',
        'instagram:DbI2s59EUda',
        's33vwak',
    ),
    (
        'https://www.pinterest.com/pin/698832067226891859/',
        'pinterest:698832067226891859',
        'nbmf6wa',
    ),
]


@pytest.mark.parametrize(('url', 'source', 'code'), KNOWN)
def test_code_matches_the_worker(url: str, source: str, code: str) -> None:
    """Same key, same SHA-256 slice, same alphabet -- same code."""
    assert shortlinks.source_of(url) == source
    assert shortlinks.code_of(source) == code
    network = source.partition(':')[0]
    assert shortlinks.short_url(url, DOMAIN) == (
        f'https://{network}.{DOMAIN}/{code}'
    )


@pytest.mark.parametrize(
    ('url', 'source'),
    [
        # The tails the networks append do not change the id.
        (
            (
                'https://www.tiktok.com/@da_eto_tyoma/video/'
                '7655351786026306838?utm_campaign=tt4d_open_api&utm_source=1'
            ),
            'tiktok:7655351786026306838',
        ),
        ('https://youtu.be/BwCeG2Qh1Ag?si=abc', 'youtube:BwCeG2Qh1Ag'),
        ('https://youtube.com/shorts/BwCeG2Qh1Ag', 'youtube:BwCeG2Qh1Ag'),
        ('https://m.youtube.com/watch?v=BwCeG2Qh1Ag', 'youtube:BwCeG2Qh1Ag'),
        ('https://instagram.com/reels/DbI2s59EUda', 'instagram:DbI2s59EUda'),
        (
            'https://at.pinterest.com/pin/698832067226891859',
            'pinterest:698832067226891859',
        ),
    ],
)
def test_source_survives_url_variants(url: str, source: str) -> None:
    """Mobile hosts, youtu.be, shorts paths and tracking tails all resolve."""
    assert shortlinks.source_of(url) == source


@pytest.mark.parametrize(
    'url',
    [
        'https://vm.tiktok.com/ZN8yBQha3/',  # an intermediary, not a video
        'https://www.tiktok.com/@da_eto_tyoma',  # a profile
        'https://www.youtube.com/@da_eto_tyoma',  # a channel
        'https://www.youtube.com/watch?v=short',  # not an 11-letter id
        'https://t.me/da_eto_tyoma',  # another service entirely
        'https://daetotyoma.com/',
        'not a url',
        '',
    ],
)
def test_unknown_addresses_pass_through(url: str) -> None:
    """Anything that is not one of the four networks' videos is untouched."""
    assert shortlinks.source_of(url) == ''
    assert shortlinks.short_url(url, DOMAIN) == url


def test_empty_domain_switches_the_rewrite_off() -> None:
    """No domain configured: even a recognised video keeps its URL."""
    url = KNOWN[0][0]
    assert shortlinks.short_url(url, '') == url


def _group(url: str) -> Group:
    """One TikTok item carrying ``url``, titled for the post."""
    item = Item('tiktok', 'tiktok', 'Cat meets cucumber', url, '', '', 1)
    return Group('Cat meets cucumber', {'tiktok': item})


def test_compose_links_to_the_short_address() -> None:
    """The rendered post carries the short address, never the raw URL."""
    consts = config.load_constants(config.CONSTANTS_PATH)
    assert consts.short_link_domain == DOMAIN  # the shipped constants set it
    url, _source, code = KNOWN[0]
    message = render.compose(_group(url), ('tiktok',), consts)
    links = [span.ref for span in message.spans if span.kind == LINK]
    assert links == [f'https://tiktok.{DOMAIN}/{code}']


def test_compose_keeps_raw_links_when_off() -> None:
    """With the domain blank the post links exactly where the item did."""
    consts = config.load_constants(config.CONSTANTS_PATH)
    off = replace(consts, short_link_domain='')
    url = KNOWN[0][0]
    message = render.compose(_group(url), ('tiktok',), off)
    links = [span.ref for span in message.spans if span.kind == LINK]
    assert links == [url]
