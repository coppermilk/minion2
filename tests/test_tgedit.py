# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The channel editor: parse, dry-run, limits, never-touch, apply, check.

Pure logic (core/tgedit), the worker over a fake account and a fake site
feed (glue/tgedit), and the dry check over a ``telegram`` snapshot
(dev/tgedit_check). The dry run is the heart of it: ``old`` that
matches the post's current text is applied, anything else is skipped and
said so.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Self
from typing import cast

import pytest

from minion_core.adapters import feed as feeds
from minion_core.adapters import userchat
from minions.userbot.core import config
from minions.userbot.core import relink
from minions.userbot.core import tgedit
from minions.userbot.dev import tgedit_check
from minions.userbot.glue import tgedit as worker
from tests.conftest import install_telethon_stub

CHAT = 't.me/da_eto_tyoma_chat'
WATCH = 'Watch'
GUARD = tgedit.Guard(urls=(CHAT,), link_texts=(WATCH,))
AUTHOR = 'audience-head'
CHANNEL = -100
NOW = 1_800_000_000.0
PACKAGE = '475-cleanup'
PAUSE = 10.0
CAT = '5359648353913167803'
"""A premium-emoji id the catalog knows (the first of the constants)."""
STRANGER = '1'
"""An id no catalog carries."""
FIRE = chr(0x1F525)
"""A non-BMP glyph: one character, two UTF-16 units."""
CONSTANT_EMOJI = (('love', CAT, FIRE),)
GUARD_EMOJI = tgedit.Guard(
    urls=(CHAT,), link_texts=(WATCH,), emoji=frozenset({CAT})
)
EMPTY = tgedit.Outcome(tgedit.Item(1, tgedit.EDIT, 'x'), tgedit.DRY_OK)


def _pkg(items: object, **over: object) -> dict[str, object]:
    """Return a package; ``over`` replaces top-level keys."""
    body = {'id': PACKAGE, 'author': AUTHOR, 'why': 'clean up', 'edits': items}
    return {**body, **over}


def _feed(*packages: object, **over: object) -> str:
    """Return the site feed's text; ``over`` replaces top-level keys."""
    rules = {'max_deletes': 15, 'min_pause_sec': 10, 'apply_once': True}
    body = {'version': 1, 'rules': rules, 'packages': list(packages)}
    return json.dumps({**body, **over})


def _parse(items: object, **over: object) -> tgedit.Package:
    """Parse one package with id PACKAGE."""
    return tgedit.parse(_pkg(items, **over))


Ent = tuple[int, int, str, str]
"""An entity: offset, length, kind, address ('' for a non-link)."""


def _post(text: str, *ents: Ent) -> tgedit.Post:
    """Return a pure post (id 7) with these entities."""
    return tgedit.Post(
        7,
        text,
        tuple((o, n) for o, n, _, _ in ents),
        tuple(kind for _, _, kind, _ in ents),
        tuple((i, url) for i, (_, _, _, url) in enumerate(ents) if url),
    )


def _edit(old: str, new: str = '') -> tgedit.Item:
    """Return an edit of post 7."""
    return tgedit.Item(7, tgedit.EDIT, old, new)


def _link(old: str, new: str) -> tgedit.Item:
    """Return a link change on post 7."""
    return tgedit.Item(7, tgedit.LINK, old, new)


def _status(plan: tgedit.PostPlan) -> list[tuple[str, str]]:
    """Return each outcome's (status, reason)."""
    return [(one.status, one.reason) for one in plan.outcomes]


# -------------------------------------------------------------- parsing


def test_a_package_block_parses_into_items() -> None:
    """Every action of the protocol reads; post 0 is the channel."""
    package = _parse(
        [
            {'post': 0, 'action': 'desc', 'new': '\nfor brands'},
            {'post': 0, 'action': 'pin_new', 'new': 'hello'},
            {'post': 1142, 'action': 'delete'},
            {'post': 1006, 'action': 'delete', 'only_if_empty': True},
            {'post': 944, 'action': 'edit', 'old': 'a', 'new': 'b'},
            {'post': 944, 'action': 'link', 'old': 'u', 'new': 'v'},
        ]
    )
    items = package.items
    assert (package.id, package.author, package.why) == (
        PACKAGE,
        AUTHOR,
        'clean up',
    )
    assert [one.action for one in items] == [
        'desc',
        'pin_new',
        'delete',
        'delete',
        'edit',
        'link',
    ]
    assert items[3].only_if_empty
    assert not items[2].only_if_empty


_DEL = [{'post': 1, 'action': 'delete'}]


@pytest.mark.parametrize(
    ('raw', 'says'),
    [
        (_pkg(_DEL, extra=1), 'unknown key'),
        (_pkg([]), 'non-empty list'),
        (_pkg(_DEL, author=' '), 'author'),
        (_pkg(_DEL, why='a\nb'), 'one line'),
        (_pkg([{'post': 1, 'action': 'move'}]), "action 'move'"),
        (_pkg([{'post': 1, 'action': 'delete', 'x': 1}]), 'edit 1: unknown'),
        (_pkg([{'post': 5, 'action': 'desc', 'new': 'x'}]), 'needs post 0'),
        (_pkg([{'post': 0, 'action': 'edit', 'old': 'x'}]), 'needs post >0'),
        (_pkg([{'post': 1, 'action': 'edit'}]), 'non-empty old/new'),
        (_pkg([{'post': -1, 'action': 'delete'}]), 'whole number'),
        (_pkg([{'post': True, 'action': 'delete'}]), 'whole number'),
    ],
)
def test_a_broken_package_is_refused_whole(
    raw: dict[str, object], says: str
) -> None:
    """A typo stops the package before anything is touched."""
    with pytest.raises(tgedit.PackageError, match=None) as caught:
        tgedit.parse(raw)
    assert says in str(caught.value)
    assert str(caught.value).startswith(PACKAGE)


def test_a_package_is_an_object() -> None:
    """A list where a package should be is refused."""
    with pytest.raises(tgedit.PackageError, match='JSON object'):
        tgedit.parse([])


@pytest.mark.parametrize(
    'name', ['cleanup', '475', '475-Cleanup', '475-', 475, None]
)
def test_a_package_id_is_a_number_dash_word(name: object) -> None:
    """The id is what ``tgedit.json`` remembers."""
    with pytest.raises(tgedit.PackageError, match='<number>-<word>'):
        tgedit.parse(_pkg(_DEL, id=name))


def test_fifteen_deletions_pass_sixteen_refuse_the_package() -> None:
    """At most 15 deletions a package; the config may not raise that."""
    fifteen = tuple(tgedit.Item(n, tgedit.DELETE) for n in range(1, 16))
    tgedit.check_limits(fifteen, 15)
    sixteen = (*fifteen, tgedit.Item(99, tgedit.DELETE))
    with pytest.raises(tgedit.PackageError, match='16 deletions'):
        tgedit.check_limits(sixteen, 99)


# ----------------------------------------------------------------- feed


def test_a_good_feed_reads_its_packages_in_order() -> None:
    """The site's feed: version 1, its rules, every package."""
    feed = tgedit.parse_feed(_feed(_pkg(_DEL), _pkg(_DEL, id='476-pin')))
    assert [one.id for one in feed.packages] == [PACKAGE, '476-pin']
    assert feed.rules == tgedit.Rules(15, 10.0)


def test_an_empty_feed_has_nothing_to_do() -> None:
    """No packages is a feed, not an error."""
    assert tgedit.parse_feed(_feed()).packages == ()


@pytest.mark.parametrize(
    ('text', 'says'),
    [
        ('{"version": ', 'does not parse'),
        ('<html>busy</html>', 'does not parse'),
        ('[]', 'not a JSON object'),
        (_feed(version=2), 'version 2'),
        (_feed(version='1'), "version '1'"),
        (_feed(version=True), 'version True'),
        (json.dumps({'packages': []}), 'version None'),
        (_feed(packages={}), 'no packages list'),
        (_feed(_pkg(_DEL), _pkg(_DEL)), 'more than once'),
        (_feed(_pkg(_DEL), _pkg([], id='476-x')), '476-x: edits'),
        (_feed(rules=[]), 'rules are not an object'),
        (_feed(rules={'max_deletes': 'all'}), 'max_deletes'),
        (_feed(rules={'min_pause_sec': None}), 'min_pause_sec'),
    ],
)
def test_a_broken_feed_is_refused_whole(text: str, says: str) -> None:
    """Broken, of another version, a bad package or one id twice: none."""
    with pytest.raises(tgedit.FeedError) as caught:
        tgedit.parse_feed(text)
    assert says in str(caught.value)


def test_the_feed_rules_only_tighten_ours() -> None:
    """The stricter of the feed and the engine; never past the hard ones."""
    strict = tgedit.Rules(max_deletes=3, min_pause_sec=30)
    assert strict.tighten(15, 10) == tgedit.Rules(3, 30.0)
    loose = tgedit.Rules(max_deletes=99, min_pause_sec=0)
    assert loose.tighten(15, 10) == tgedit.Rules(15, 10.0)
    assert loose.tighten(99, 1) == tgedit.Rules(15, 10.0)
    assert tgedit.Rules(-5).tighten(15, 10).max_deletes == 0
    rules = tgedit.parse_feed(_feed(rules={})).rules
    assert rules == tgedit.Rules()


# -------------------------------------------------------------- dry run


def test_an_exact_fragment_is_replaced_and_formatting_moves_with_it() -> None:
    """``old`` matched once: replaced; a bold span after it shifts."""
    post = _post('aaa bad word. tail', (14, 4, 'other', ''))
    plan = tgedit.plan_post(post, (_edit('bad word.', 'ok.'),), GUARD)
    assert _status(plan) == [(tgedit.DRY_OK, '')]
    assert plan.text == 'aaa ok. tail'
    assert plan.spans == ((8, 4),)


def test_two_fragments_of_one_post_both_land_in_order() -> None:
    """The second item runs on the text the first one left."""
    post = _post('one two three')
    plan = tgedit.plan_post(
        post, (_edit('one ', ''), _edit('three', '3')), GUARD
    )
    assert plan.text == 'two 3'
    assert plan.changes


@pytest.mark.parametrize(
    ('old', 'reason'),
    [
        ('missing', tgedit.NOT_FOUND),
        ('ab', tgedit.AMBIGUOUS),
        ('Ab', tgedit.NOT_FOUND),  # character for character, case included
    ],
)
def test_old_must_match_the_current_text_exactly_once(
    old: str, reason: str
) -> None:
    """No match, or two, is a skip -- never a guess."""
    plan = tgedit.plan_post(_post('ab ab'), (_edit(old, 'x'),), GUARD)
    assert _status(plan) == [(tgedit.SKIPPED, reason)]
    assert not plan.changes


def test_a_fragment_cutting_across_formatting_is_skipped() -> None:
    """A cut over an entity's edge would move bold onto other letters."""
    post = _post('hello world', (3, 5, 'other', ''))
    plan = tgedit.plan_post(post, (_edit('llo w', ''),), GUARD)
    assert _status(plan) == [(tgedit.SKIPPED, tgedit.FORMATTING)]


def test_premium_emoji_are_never_touched() -> None:
    """A fragment over a custom emoji is protected (the watch buttons)."""
    post = _post('x \U0001f929 Watch', (2, 2, tgedit.EMOJI, ''))
    plan = tgedit.plan_post(post, (_edit('\U0001f929', '*'),), GUARD)
    assert _status(plan) == [(tgedit.SKIPPED, tgedit.PROTECTED)]


def test_the_chat_address_is_never_touched() -> None:
    """Neither an edit nor a link change may name the chat."""
    post = _post(f'come {CHAT}', (5, len(CHAT), relink.VISIBLE, CHAT))
    items = (_edit(CHAT, 'gone'), _link(CHAT, 'https://x'))
    plan = tgedit.plan_post(post, items, GUARD)
    assert _status(plan) == [(tgedit.SKIPPED, tgedit.PROTECTED)] * 2


def test_a_hidden_link_changes_only_its_address() -> None:
    """Every link with that address moves; the text stays."""
    old, new = 'https://youtube.com/@a', 'https://youtube.daetotyoma.com/x'
    post = _post(
        'on YT and me', (3, 2, relink.HIDDEN, old), (10, 2, relink.HIDDEN, old)
    )
    plan = tgedit.plan_post(post, (_link(old, new),), GUARD)
    assert _status(plan) == [(tgedit.DRY_OK, '')]
    assert plan.urls == {0: new, 1: new}
    assert plan.text == 'on YT and me'


def test_a_link_counts_every_address_it_moves() -> None:
    """Two links with the same ``old`` (post 1045): both move, count 2."""
    old, new = 'https://youtube.com/@a', 'https://youtube.daetotyoma.com/x'
    post = _post(
        'on YT and me', (3, 2, relink.HIDDEN, old), (10, 2, relink.HIDDEN, old)
    )
    plan = tgedit.plan_post(post, (_link(old, new),), GUARD)
    assert [one.count for one in plan.outcomes] == [2]
    applied = worker._landed(plan.outcomes, landed=True)
    assert [(one.status, one.count) for one in applied] == [
        (tgedit.APPLIED, 2)
    ]


def test_a_visible_link_changes_its_text_too() -> None:
    """A url entity IS its text: the text is replaced, the span grows."""
    old, new = (
        'https://vm.tiktok.com/Z/',
        'https://tiktok.daetotyoma.com/a94edx5',
    )
    post = _post(f'see {old}', (4, len(old), relink.VISIBLE, old))
    plan = tgedit.plan_post(post, (_link(old, new),), GUARD)
    assert plan.text == f'see {new}'
    assert plan.spans == ((4, len(new)),)


def test_a_watch_link_is_never_readdressed() -> None:
    """A link whose words are a protected label stays as it is."""
    url = 'https://tiktok.com/v/1'
    post = _post('Watch', (0, 5, relink.HIDDEN, url))
    plan = tgedit.plan_post(post, (_link(url, 'https://x'),), GUARD)
    assert _status(plan) == [(tgedit.SKIPPED, tgedit.PROTECTED)]


def test_a_link_that_is_not_there_is_skipped() -> None:
    """A link's ``old`` must be the address of one of its links."""
    post = _post('x', (0, 1, relink.HIDDEN, 'https://a'))
    plan = tgedit.plan_post(post, (_link('https://b', 'https://c'),), GUARD)
    assert _status(plan) == [(tgedit.SKIPPED, tgedit.NOT_FOUND)]


def test_deletions_check_the_post_first() -> None:
    """Missing, non-empty under only_if_empty, chat inside, or contradicted."""
    gone = tgedit.Item(7, tgedit.DELETE)
    empty_only = tgedit.Item(7, tgedit.DELETE, only_if_empty=True)
    assert tgedit.plan_post(_post('bye'), (gone,), GUARD).delete
    assert _status(tgedit.plan_post(None, (gone,), GUARD)) == [
        (tgedit.SKIPPED, tgedit.NO_POST)
    ]
    assert _status(tgedit.plan_post(_post('x'), (empty_only,), GUARD)) == [
        (tgedit.SKIPPED, tgedit.NOT_EMPTY)
    ]
    assert tgedit.plan_post(_post(''), (empty_only,), GUARD).delete
    assert _status(tgedit.plan_post(_post(CHAT), (gone,), GUARD)) == [
        (tgedit.SKIPPED, tgedit.PROTECTED)
    ]
    both = tgedit.plan_post(_post('x'), (gone, _edit('x', 'y')), GUARD)
    assert {one.reason for one in both.outcomes} == {tgedit.CONFLICT}


def test_a_description_append_is_once_and_within_the_limit() -> None:
    """Appended once; a second time is already done; over 255 is refused."""
    item = tgedit.Item(0, tgedit.DESC, new='\nfor brands')
    assert tgedit.plan_about('about', item, GUARD) == ('about\nfor brands', '')
    assert tgedit.plan_about('about\nfor brands', item, GUARD)[1] == (
        tgedit.ALREADY
    )
    long = 'x' * tgedit.ABOUT_LIMIT
    assert tgedit.plan_about(long, item, GUARD)[1] == tgedit.TOO_LONG


def test_a_new_pin_goes_out_once() -> None:
    """The same text already among the newest posts is not posted again."""
    item = tgedit.Item(0, tgedit.PIN_NEW, new='for brands')
    assert tgedit.plan_pin(['other'], item, GUARD) == ''
    assert tgedit.plan_pin(['for brands '], item, GUARD) == tgedit.ALREADY


def test_the_report_names_each_post_and_its_outcome() -> None:
    """``post N . action -> outcome (reason)``, labels from the config."""
    lines = tgedit.report(
        [
            tgedit.Outcome(_edit('x'), tgedit.APPLIED),
            tgedit.Outcome(_edit('y'), tgedit.SKIPPED, tgedit.NOT_FOUND),
        ],
        {'skipped': 'SKIP'},
    )
    assert lines == [
        'post 7 . edit -> applied',
        'post 7 . edit -> SKIP (not_found)',
    ]


def test_the_report_says_how_many_links_one_item_moved() -> None:
    """One replacement says nothing extra; two or more say the number."""
    item = _link('https://a', 'https://b')
    lines = tgedit.report(
        [
            tgedit.Outcome(item, tgedit.APPLIED, count=2),
            tgedit.Outcome(item, tgedit.APPLIED),
        ],
        {'replaced': 'N'},
    )
    assert lines == [
        'post 7 . link -> applied, N 2',
        'post 7 . link -> applied',
    ]


# ------------------------------------------- premium emoji in new text

BUNNY = chr(0x1F430)
"""A second non-BMP glyph, so two glyphs never make a fragment ambiguous."""


def _tag(ident: str, glyph: str = FIRE) -> str:
    """Return one ``<tg-emoji>`` tag."""
    return f'<tg-emoji emoji-id="{ident}">{glyph}</tg-emoji>'


def _units16(text: str) -> int:
    """Length of ``text`` in UTF-16 units, as the test counts it."""
    return len(text.encode('utf-16-le')) // 2


def test_an_edit_turns_a_tag_into_a_premium_emoji_over_its_glyph() -> None:
    """The glyph lands in the text, the entity over it in UTF-16 units."""
    post = _post(f'{FIRE} hi there')
    plan = tgedit.plan_post(
        post, (_edit('there', f'{_tag(CAT, BUNNY)} there'),), GUARD_EMOJI
    )
    assert plan.outcomes[0].status == tgedit.DRY_OK
    assert plan.text == f'{FIRE} hi {BUNNY} there'
    assert plan.emoji == ((_units16(f'{FIRE} hi '), 2, CAT),)


def test_an_id_outside_the_catalog_skips_the_edit() -> None:
    """An unknown id is never sent: not as an entity, not as a glyph."""
    plan = tgedit.plan_post(
        _post('hi there'), (_edit('there', _tag(STRANGER)),), GUARD_EMOJI
    )
    assert plan.outcomes[0].reason == tgedit.UNKNOWN_EMOJI
    assert not plan.changes
    assert plan.text == 'hi there'


@pytest.mark.parametrize(
    'new',
    [
        f'<tg-emoji emoji-id="{CAT}">{FIRE}',
        f'<tg-emoji>{FIRE}</tg-emoji>',
        f'<tg-emoji emoji-id="{CAT}"></tg-emoji>',
        f"<tg-emoji emoji-id='{CAT}'>{FIRE}</tg-emoji>",
        f'{FIRE}</tg-emoji>',
    ],
    ids=['unclosed', 'no-id', 'no-glyph', 'single-quotes', 'stray-close'],
)
def test_broken_markup_skips_the_edit(new: str) -> None:
    """A tag the reader cannot read is skipped, never sent as raw markup."""
    plan = tgedit.plan_post(
        _post('hi there'), (_edit('there', new),), GUARD_EMOJI
    )
    assert plan.outcomes[0].reason == tgedit.MARKUP
    assert not plan.changes


def test_an_added_emoji_moves_with_later_edits_and_is_then_locked() -> None:
    """Items apply in order: a later fragment shifts it, cannot touch it."""
    items = (
        _edit('end', f'end {_tag(CAT, BUNNY)}'),
        _edit('hi', 'hello'),
        _edit(BUNNY, 'x'),
    )
    plan = tgedit.plan_post(_post('hi, the end'), items, GUARD_EMOJI)
    assert [one.reason for one in plan.outcomes] == ['', '', tgedit.PROTECTED]
    assert plan.text == f'hello, the end {BUNNY}'
    assert plan.emoji == ((len('hello, the end '), 2, CAT),)


def test_a_pin_carries_its_emoji_and_is_compared_without_tags() -> None:
    """pin_new reads the tags; 'already out' compares the plain text."""
    item = tgedit.Item(0, tgedit.PIN_NEW, new=f'new {_tag(CAT)}')
    assert tgedit.plan_pin([], item, GUARD_EMOJI) == ''
    assert tgedit.plan_pin([f'new {FIRE}'], item, GUARD_EMOJI) == (
        tgedit.ALREADY
    )
    stranger = tgedit.Item(0, tgedit.PIN_NEW, new=_tag(STRANGER))
    assert tgedit.plan_pin([], stranger, GUARD_EMOJI) == tgedit.UNKNOWN_EMOJI
    new = tgedit.read_markup(item.new, GUARD_EMOJI.emoji)
    assert new.text == f'new {FIRE}'
    assert [(s.at, s.length, s.ref) for s in new.spans] == [(4, 1, CAT)]


def test_a_description_cannot_carry_premium_emoji() -> None:
    """The about is plain text: a tag there is skipped, not appended raw."""
    item = tgedit.Item(0, tgedit.DESC, new=f' {_tag(CAT)}')
    assert tgedit.plan_about('about', item, GUARD_EMOJI) == ('', tgedit.MARKUP)


@dataclass
class _Glyph:
    """A vendor custom-emoji entity, as the adapter reads and copies it."""

    offset: int
    length: int
    document_id: int


@dataclass
class _Message:
    """A vendor message: text and entities, no media."""

    id: int
    message: str
    entities: list[object]
    date: object = None
    media: object = None


def test_an_edit_keeps_every_premium_emoji_of_the_post() -> None:
    """Three emoji, an edit between them: all three, same ids, same glyphs.

    Through the adapter's own read (``_linked``) and copy (``_reentities``),
    as the engine writes a post, plus one emoji the edit adds.
    """
    install_telethon_stub()
    text = f'{FIRE} one {FIRE} two {FIRE} {WATCH}'
    at = [_units16(text[:i]) for i, c in enumerate(text) if c == FIRE]
    raw = _Message(
        7,
        text,
        [
            _Glyph(o, 2, ident)
            for o, ident in zip(at, (11, 22, 33), strict=True)
        ],
    )
    read = userchat._linked(raw)
    plan = tgedit.plan_post(
        worker.to_post(read),
        (_edit('one', f'number one {_tag(CAT, BUNNY)}'),),
        GUARD_EMOJI,
    )
    assert plan.outcomes[0].status == tgedit.DRY_OK
    edit = userchat.Relinked(plan.text, plan.spans, plan.urls, plan.emoji)
    ents = userchat._reentities(read, edit)
    units = plan.text.encode('utf-16-le')
    shown = [
        units[e.offset * 2 : (e.offset + e.length) * 2].decode('utf-16-le')
        for e in cast('list[_Glyph]', ents)
    ]
    ids = [int(cast('_Glyph', e).document_id) for e in ents]
    assert ids == [11, int(CAT), 22, 33]
    assert shown == [FIRE, BUNNY, FIRE, FIRE]
    assert [e.document_id for e in raw.entities] == [11, 22, 33]  # type: ignore[attr-defined]


# ---------------------------------------------------------- the worker


@dataclass
class _Account:
    """A fake account: posts by id, and a log of every write."""

    posts_by_id: dict[int, userchat.Linked]
    granted: frozenset[str] | None = frozenset(
        {'post', 'edit', 'delete', 'pin', 'info'}
    )
    description: str = 'about'
    unreadable: bool = False
    writes: list[tuple[str, int]] = field(default_factory=list)
    sets: list[userchat.EmojiSet] | None = field(default_factory=list)
    edits: list[userchat.Relinked] = field(default_factory=list)
    sent: list[userchat.Text] = field(default_factory=list)

    async def emoji_sets(self) -> list[userchat.EmojiSet] | None:
        """Return the installed custom-emoji sets (None: unreadable)."""
        return self.sets

    async def rights(self, chat: int) -> frozenset[str] | None:
        """Return the granted rights."""
        return self.granted

    async def posts(
        self, chat: int, ids: list[int]
    ) -> dict[int, userchat.Linked] | None:
        """Return the asked posts that exist."""
        if self.unreadable:
            return None
        return {i: self.posts_by_id[i] for i in ids if i in self.posts_by_id}

    async def relink(
        self, chat: int, post: userchat.Linked, edit: userchat.Relinked
    ) -> bool:
        """Record an edit."""
        self.writes.append(('edit', post.id))
        self.edits.append(edit)
        self.posts_by_id[post.id] = userchat.Linked(
            post.id, edit.text, post.date, edit.spans, post.links
        )
        return True

    async def delete(self, chat: int, msg_id: int) -> bool:
        """Record a deletion."""
        self.writes.append(('delete', msg_id))
        return self.posts_by_id.pop(msg_id, None) is not None

    async def about(self, chat: int) -> str | None:
        """Return the description."""
        return self.description

    async def set_about(self, chat: int, about: str) -> bool:
        """Record a description change."""
        self.writes.append(('about', 0))
        self.description = about
        return True

    async def linked(self, chat: int, limit: int) -> list[userchat.Linked]:
        """Return the newest posts."""
        return list(self.posts_by_id.values())[:limit]

    async def send(self, chat: int, text: userchat.Text) -> int:
        """Record a new post."""
        self.writes.append(('send', 900))
        self.sent.append(text)
        self.posts_by_id[900] = userchat.Linked(900, text.body, NOW, (), ())
        return 900

    async def pin(self, chat: int, msg_id: int) -> bool:
        """Record a pin."""
        self.writes.append(('pin', msg_id))
        return True


def _linked(ident: int, text: str) -> userchat.Linked:
    """Return an adapter post without entities."""
    return userchat.Linked(ident, text, NOW, (), (), kinds=())


ITEMS: list[dict[str, object]] = [
    {'post': 0, 'action': 'desc', 'new': '\nfor brands'},
    {'post': 0, 'action': 'pin_new', 'new': 'hello brands'},
    {'post': 1142, 'action': 'delete'},
    {'post': 1167, 'action': 'edit', 'old': 'gov and blog', 'new': 'blog'},
    {'post': 944, 'action': 'edit', 'old': 'not there', 'new': 'x'},
]


@dataclass
class _Site:
    """A fake site feed: its text, or a failure to reach it."""

    text: str
    down: bool = False
    reads: int = 0

    async def read(self) -> str:
        """Return the feed, or fail as the adapter does."""
        self.reads += 1
        if self.down:
            msg = 'https://site/tgedit.json: ConnectionError'
            raise feeds.FeedUnreachableError(msg)
        return self.text


def _world(items: object = None) -> tuple[_Account, _Site]:
    """Put one package in the feed; return the account and the site."""
    site = _Site(_feed(_pkg(ITEMS if items is None else items)))
    return _Account(
        {
            1142: _linked(1142, 'old tour'),
            1167: _linked(1167, 'it is gov and blog.'),
            944: _linked(944, 'support'),
            1000: _linked(1000, 'not in the package'),
        }
    ), site


def _editor(
    tmp_path: Path, world: tuple[_Account, _Site], mode: str
) -> tuple[worker.ChannelEditor, list[float]]:
    """Build an editor in ``mode``; return it and the pauses it took."""
    slept: list[float] = []

    async def sleep(sec: float) -> None:
        slept.append(sec)

    account, site = world
    params = worker.EditParams(
        mode=mode, pause_sec=PAUSE, guard=GUARD, emoji=CONSTANT_EMOJI
    )
    editor = worker.ChannelEditor(
        worker.EditDeps(
            account=cast('userchat.Account', account),
            channel=lambda: CHANNEL,
            feed=site.read,
            path=tmp_path / worker.STATE_FILE,
            journal=tmp_path / worker.JOURNAL_FILE,
            catalog=tmp_path / worker.CATALOG_FILE,
            params=params,
            sleep=sleep,
        )
    )
    editor.load()
    return editor, slept


def _journal(tmp_path: Path, what: str) -> list[dict[str, object]]:
    """Return the journal's rows of one kind."""
    path = tmp_path / worker.JOURNAL_FILE
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text('utf-8').splitlines()]
    return [row for row in rows if row['what'] == what]


def test_off_reads_nothing(tmp_path: Path) -> None:
    """Off reads neither the feed nor the channel, writes no journal."""
    world = _world()
    editor, _ = _editor(tmp_path, world, 'off')
    assert asyncio.run(editor.tick(NOW)) == 0
    assert world[0].writes == []
    assert world[1].reads == 0
    assert not (tmp_path / worker.JOURNAL_FILE).exists()


def test_dry_logs_and_journals_every_item_once_and_writes_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Dry: every item's outcome goes to the log and the journal, once."""
    world = _world()
    editor, _ = _editor(tmp_path, world, 'dry')
    with caplog.at_level(logging.INFO, logger='userbot'):
        assert asyncio.run(editor.tick(NOW)) == 1
    assert world[0].writes == []
    assert f'{PACKAGE}: post 1167 . edit -> dry_ok' in caplog.text
    assert f'{PACKAGE}: post 944 . edit -> skipped (not_found)' in caplog.text
    rows = _journal(tmp_path, 'outcome')
    assert len(rows) == len(ITEMS)
    assert {row['mode'] for row in rows} == {'dry'}
    assert asyncio.run(editor.tick(NOW)) == 0
    assert len(_journal(tmp_path, 'outcome')) == len(ITEMS)


def test_on_applies_journals_paces_and_reports(tmp_path: Path) -> None:
    """On: what passed is written, 10 s apart, after its before-image."""
    world = _world()
    account = world[0]
    editor, slept = _editor(tmp_path, world, 'on')
    assert asyncio.run(editor.tick(NOW)) == 1
    assert account.writes == [
        ('delete', 1142),
        ('edit', 1167),
        ('about', 0),
        ('send', 900),
        ('pin', 900),
    ]
    assert slept == [PAUSE] * (len(account.writes) - 1)
    assert account.posts_by_id[1167].text == 'it is blog.'
    assert account.posts_by_id[1000].text == 'not in the package'
    assert account.description == 'about\nfor brands'
    before = _journal(tmp_path, 'post')
    assert [row.get('text') for row in before] == [
        'old tour',
        'it is gov and blog.',
    ]
    outcomes = [
        (r['post'], r['status']) for r in _journal(tmp_path, 'outcome')
    ]
    assert outcomes.count((0, tgedit.APPLIED)) == 2  # noqa: PLR2004
    assert (944, tgedit.SKIPPED) in outcomes
    assert len(outcomes) == len(ITEMS)


def test_a_package_id_is_applied_once_across_restarts(tmp_path: Path) -> None:
    """The same id again -- next tick or after a restart -- writes nothing."""
    world = _world()
    editor, _ = _editor(tmp_path, world, 'on')
    asyncio.run(editor.tick(NOW))
    writes = list(world[0].writes)
    assert asyncio.run(editor.tick(NOW)) == 0
    again, _ = _editor(tmp_path, world, 'on')
    assert asyncio.run(again.tick(NOW)) == 0
    assert world[0].writes == writes


def test_a_new_package_in_the_feed_is_the_only_one_handled(
    tmp_path: Path,
) -> None:
    """A feed that grows: only the new id runs; the old one stays done."""
    world = _world()
    editor, _ = _editor(tmp_path, world, 'on')
    asyncio.run(editor.tick(NOW))
    new = _pkg([{'post': 944, 'action': 'delete'}], id='476-tidy')
    world[1].text = _feed(_pkg(ITEMS), new)
    assert asyncio.run(editor.tick(NOW)) == 1
    assert world[0].writes[-1] == ('delete', 944)
    assert editor.state.done == {PACKAGE, '476-tidy'}


def test_a_package_gone_from_the_feed_is_not_applied(tmp_path: Path) -> None:
    """Dry-reported, then dropped from the feed: on never applies it."""
    world = _world()
    dry, _ = _editor(tmp_path, world, 'dry')
    asyncio.run(dry.tick(NOW))
    world[1].text = _feed()
    on, _ = _editor(tmp_path, world, 'on')
    assert asyncio.run(on.tick(NOW)) == 0
    assert world[0].writes == []


def test_a_dry_reported_package_is_still_applied_once_on(
    tmp_path: Path,
) -> None:
    """Dry, then on: the dry report does not count as applied."""
    world = _world()
    dry, _ = _editor(tmp_path, world, 'dry')
    asyncio.run(dry.tick(NOW))
    on, _ = _editor(tmp_path, world, 'on')
    assert asyncio.run(on.tick(NOW)) == 1
    assert world[0].writes
    assert worker.read_state(tmp_path / worker.STATE_FILE).done == {PACKAGE}


@pytest.mark.parametrize(
    'case',
    [
        ('', True, 'ConnectionError'),
        ('{', False, 'does not parse'),
        (_feed(version=2), False, 'version 2'),
        (_feed(_pkg(ITEMS), _pkg(ITEMS)), False, 'more than once'),
    ],
    ids=['unreachable', 'broken', 'version-2', 'id-twice'],
)
def test_a_feed_it_cannot_trust_skips_the_tick_and_says_so(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    case: tuple[str, bool, str],
) -> None:
    """Nothing read, written or remembered; one log line; next tick tries."""
    text, down, says = case
    world = _world()
    world[1].text, world[1].down = text, down
    editor, _ = _editor(tmp_path, world, 'on')
    with caplog.at_level(logging.WARNING, logger='userbot'):
        assert asyncio.run(editor.tick(NOW)) == 0
    assert 'feed skipped this tick' in caplog.text
    assert says in caplog.text
    assert world[0].writes == []
    assert not (tmp_path / worker.JOURNAL_FILE).exists()
    assert editor.state == worker.EditState()
    world[1].text, world[1].down = _feed(_pkg(ITEMS)), False
    assert asyncio.run(editor.tick(NOW)) == 1


def test_the_feed_rules_slow_the_writes_down(tmp_path: Path) -> None:
    """A feed asking for 30 s between writes gets 30 s, not our 10."""
    world = _world()
    world[1].text = _feed(_pkg(ITEMS), rules={'min_pause_sec': 30})
    editor, slept = _editor(tmp_path, world, 'on')
    asyncio.run(editor.tick(NOW))
    assert slept
    assert set(slept) == {30.0}


def test_the_feed_rules_cap_deletions_lower(tmp_path: Path) -> None:
    """A feed allowing one deletion refuses a package with two."""
    two = [
        {'post': 1142, 'action': 'delete'},
        {'post': 944, 'action': 'delete'},
    ]
    world = _world(two)
    world[1].text = _feed(_pkg(two), rules={'max_deletes': 1})
    editor, _ = _editor(tmp_path, world, 'on')
    asyncio.run(editor.tick(NOW))
    assert world[0].writes == []
    assert '2 deletions' in str(_journal(tmp_path, 'refused')[0]['why'])


def test_too_many_deletions_refuse_the_package(tmp_path: Path) -> None:
    """Sixteen deletions: refused whole, journaled, nothing deleted."""
    many = [{'post': n, 'action': 'delete'} for n in range(1, 17)]
    world = _world(many)
    world[1].text = _feed(_pkg(many), rules={'max_deletes': 99})
    editor, _ = _editor(tmp_path, world, 'on')
    asyncio.run(editor.tick(NOW))
    assert world[0].writes == []
    assert '16 deletions' in str(_journal(tmp_path, 'refused')[0]['why'])


def test_without_rights_every_item_says_so(tmp_path: Path) -> None:
    """An account that may not edit the channel edits nothing."""
    world = _world()
    world[0].granted = frozenset()
    editor, _ = _editor(tmp_path, world, 'on')
    asyncio.run(editor.tick(NOW))
    assert world[0].writes == []
    reasons = [row['reason'] for row in _journal(tmp_path, 'outcome')]
    assert reasons == [tgedit.NO_RIGHTS] * len(ITEMS)


def test_an_unreadable_channel_retries_later(tmp_path: Path) -> None:
    """A failed read is no 'post gone': the package stays pending."""
    world = _world()
    world[0].unreadable = True
    editor, _ = _editor(tmp_path, world, 'on')
    with pytest.raises(worker.ChannelUnreadableError):
        asyncio.run(editor.tick(NOW))
    assert editor.state.done == set()
    world[0].unreadable = False
    assert asyncio.run(editor.tick(NOW)) == 1


def test_an_old_state_file_with_board_ids_reads_empty(tmp_path: Path) -> None:
    """Board note numbers from the old engine never match a package id."""
    path = tmp_path / worker.STATE_FILE
    path.write_text('{"done": [475], "dry": [475]}', 'utf-8')
    assert worker.read_state(path) == worker.EditState()


# ------------------------------------------------------- configuration


def test_the_shipped_constants_keep_the_editor_safe() -> None:
    """Hard limits hold, the chat and the watch links are protected."""
    params = worker.load_edit_params(config.read_json(config.CONSTANTS_PATH))
    assert params.mode in worker.MODES
    assert params.max_deletes == tgedit.MAX_DELETES
    assert params.pause_sec >= tgedit.MIN_PAUSE_SEC
    assert CHAT in params.guard.urls
    assert params.guard.link_texts
    assert params.feed_url == worker.FEED_URL
    assert worker.load_edit_params({}).feed_url == worker.FEED_URL


def test_the_hard_limits_cannot_be_loosened() -> None:
    """A constants file cannot raise the deletions cap or shorten the pause."""
    loose = {'engines': {'tgedit': {'max_deletes': 99, 'pause_sec': 1}}}
    params = worker.load_edit_params(loose)
    assert params.max_deletes == tgedit.MAX_DELETES
    assert params.pause_sec == tgedit.MIN_PAUSE_SEC
    assert worker.load_edit_params({}).mode == 'off'


def test_the_engine_has_no_door_to_the_board() -> None:
    """Approval is the merge: neither half of the engine reads a board."""
    for module in (tgedit, worker, feeds, tgedit_check):
        source = Path(str(module.__file__)).read_text('utf-8').lower()
        assert 'board' not in source, module.__name__


# ------------------------------------------------------- the dry check


def _snapshot(*posts: dict[str, object]) -> str:
    """Return a ``telegram`` answer as the MCP's JSON-RPC envelope."""
    body = json.dumps({'channel': 'c', 'posts': list(posts)})
    text = f'{len(posts)} posts\n\n{body}'
    return json.dumps(
        {'result': {'content': [{'type': 'text', 'text': text}]}}
    )


def _snap(ident: int, text: str, *links: dict[str, str]) -> dict[str, object]:
    """Return one snapshot post with text links."""
    rows = [{**one, 'kind': one.get('kind', 'text')} for one in links]
    return {'id': ident, 'text': text, 'links': rows, 'video': False}


def test_the_snapshot_reads_as_envelope_or_text_and_refuses_the_rest() -> None:
    """The curl answer and the tool's text read alike; a stranger raises."""
    envelope = _snapshot(_snap(1, 'a'))
    text = json.loads(envelope)['result']['content'][0]['text']
    assert tgedit_check.read_snapshot(envelope)[0]['id'] == 1
    assert tgedit_check.read_snapshot(text)[0]['id'] == 1
    with pytest.raises(tgedit_check.SnapshotError, match='no posts'):
        tgedit_check.read_snapshot('{"board": "admin"}')


def test_the_dry_check_says_matched_missing_twice_and_no_post() -> None:
    """Each item reads as the engine's planner would read it."""
    package = _parse(
        [
            {'post': 1, 'action': 'edit', 'old': 'gov', 'new': 'blog'},
            {'post': 2, 'action': 'edit', 'old': 'gone', 'new': 'x'},
            {'post': 3, 'action': 'edit', 'old': 'ha', 'new': 'x'},
            {'post': 4, 'action': 'delete'},
            {'post': 0, 'action': 'desc', 'new': 'x'},
            {'post': 0, 'action': 'pin_new', 'new': 'b'},
        ]
    )
    posts = tgedit_check.read_snapshot(
        _snapshot(_snap(1, 'gov'), _snap(2, 'b'), _snap(3, 'ha ha'))
    )
    got = tgedit_check.check(package, posts, GUARD)
    assert [(one.item.post, one.status, one.reason) for one in got] == [
        (1, tgedit.DRY_OK, ''),
        (2, tgedit.SKIPPED, tgedit.NOT_FOUND),
        (3, tgedit.SKIPPED, tgedit.AMBIGUOUS),
        (4, tgedit.SKIPPED, tgedit.NO_POST),
        (0, tgedit_check.UNCHECKED, ''),
        (0, tgedit.SKIPPED, tgedit.ALREADY),
    ]


def test_the_dry_check_keeps_links_and_their_protection() -> None:
    """A link moves by address; a watch row and a swallowed link do not.

    The last case is post 1155 of package #475: the fragment holds the
    whole link, so the engine would skip it and the check says so first.
    """
    watch = {'href': 'https://a', 'text': WATCH}
    ask = {'href': 'https://q', 'text': 'ask'}
    posts = tgedit_check.read_snapshot(
        _snapshot(
            _snap(1, 'see here', {'href': 'https://old', 'text': 'here'}),
            _snap(2, f'{WATCH} now', watch),
            _snap(3, 'hi\n\nask me', ask),
        )
    )
    package = _parse(
        [
            {'post': 1, 'action': 'link', 'old': 'https://old', 'new': 'n'},
            {'post': 2, 'action': 'link', 'old': 'https://a', 'new': 'n'},
            {'post': 3, 'action': 'edit', 'old': '\n\nask me', 'new': ''},
        ]
    )
    got = tgedit_check.check(package, posts, GUARD)
    assert [(one.status, one.reason) for one in got] == [
        (tgedit.DRY_OK, ''),
        (tgedit.SKIPPED, tgedit.PROTECTED),
        (tgedit.SKIPPED, tgedit.FORMATTING),
    ]


def test_a_post_with_media_is_not_empty() -> None:
    """only_if_empty spares a post that is only a video."""
    raw = {'id': 9, 'text': '', 'links': [], 'video': True, 'photos': 0}
    assert tgedit_check.to_post(raw).media


def test_the_dry_check_prints_a_block_per_package_of_the_feed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command prints each package's tally, then each of its items."""
    feed = tmp_path / 'tgedit.json'
    edit = [{'post': 1, 'action': 'edit', 'old': 'a', 'new': 'b'}]
    feed.write_text(_feed(_pkg(edit), _pkg(edit, id='476-x')), 'utf-8')
    snapshot = tmp_path / 'telegram.json'
    snapshot.write_text(_snapshot(_snap(1, 'a')), 'utf-8')
    assert tgedit_check.main([str(feed), str(snapshot)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith(f'{PACKAGE}: 1 items')
    assert out[2].startswith('476-x: 1 items')
    assert len(out) == 4  # noqa: PLR2004
    snapshot.write_text('not json', 'utf-8')
    assert tgedit_check.main([str(feed), str(snapshot)]) == 1
    snapshot.write_text(_snapshot(_snap(1, 'a')), 'utf-8')
    feed.write_text(_feed(version=2), 'utf-8')
    assert tgedit_check.main([str(feed), str(snapshot)]) == 1
    assert tgedit_check.main([str(tmp_path / 'none'), str(snapshot)]) == 1


def test_an_outcome_is_counted_by_status() -> None:
    """The report's first line counts outcomes by status."""
    assert tgedit.tally([EMPTY, EMPTY]) == {tgedit.DRY_OK: 2}


# ------------------------------------------------------ the feed door


@dataclass
class _Raw:
    """The body of a fake response, read in one go."""

    body: bytes

    def read(self, limit: int, *, decode_content: bool) -> bytes:
        """Return at most ``limit`` bytes."""
        assert decode_content
        return self.body[:limit]


@dataclass
class _Resp:
    """A fake ``requests`` response with a status and a body."""

    raw: _Raw
    status: int = 200

    def __enter__(self) -> Self:
        """Open the response."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the response."""

    def raise_for_status(self) -> None:
        """Fail on a non-2xx status, as requests does."""
        import requests

        if self.status >= 400:  # noqa: PLR2004 -- HTTP's error range
            raise requests.HTTPError(str(self.status))


def _serve(
    monkeypatch: pytest.MonkeyPatch, resp: _Resp | Exception
) -> list[dict[str, object]]:
    """Answer every ``requests.get`` with ``resp``; return the calls."""
    import requests

    calls: list[dict[str, object]] = []

    def get(url: str, **kw: object) -> _Resp:
        calls.append({'url': url, **kw})
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(requests, 'get', get)
    return calls


def test_the_feed_door_says_who_it_is_and_reads_utf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cloudflare turns the stdlib's client name away: ours is named."""
    calls = _serve(monkeypatch, _Resp(_Raw('{"a": "\u0451"}'.encode())))
    assert feeds.read('https://site/tgedit.json') == '{"a": "\u0451"}'
    headers = cast('dict[str, str]', calls[0]['headers'])
    assert headers['User-Agent'] == feeds.USER_AGENT
    assert calls[0]['timeout'] == feeds.TIMEOUT_SEC


@pytest.mark.parametrize(
    ('resp', 'says'),
    [
        (_Resp(_Raw(b'busy'), status=503), 'HTTPError'),
        (_Resp(_Raw(b'x' * (feeds.LIMIT_BYTES + 1))), 'larger than'),
        (_Resp(_Raw(b'\xff')), 'not UTF-8'),
    ],
    ids=['http-503', 'too-big', 'not-utf8'],
)
def test_the_feed_door_raises_instead_of_reading_nothing(
    monkeypatch: pytest.MonkeyPatch, resp: _Resp, says: str
) -> None:
    """A failed read is an error naming the address, never an empty feed."""
    _serve(monkeypatch, resp)
    with pytest.raises(feeds.FeedUnreachableError, match=says) as caught:
        feeds.read('https://site/tgedit.json')
    assert 'https://site/tgedit.json' in str(caught.value)


def test_the_feed_door_turns_a_dead_network_into_its_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No network: the worker's error, with the address and the cause."""
    import requests

    _serve(monkeypatch, requests.ConnectionError('down'))
    with pytest.raises(feeds.FeedUnreachableError, match='ConnectionError'):
        feeds.read('https://site/tgedit.json')


# ------------------------------------------------------ the emoji catalog

PACK = userchat.EmojiSet('TyomaPack', 'Tyoma', (('777', BUNNY),))


def _catalog(tmp_path: Path) -> dict[str, object]:
    """Return the written emoji catalog."""
    path = tmp_path / worker.CATALOG_FILE
    return cast('dict[str, object]', json.loads(path.read_text('utf-8')))


def _with_emoji(text: str, ident: str) -> userchat.Linked:
    """Return a post whose first glyph is a custom emoji."""
    return userchat.Linked(
        5, text, NOW, ((0, 2),), (), kinds=('emoji',), refs=(ident,)
    )


def test_the_catalog_merges_sets_channel_and_constants(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One row per id with every source; logged and written to a file."""
    world = _world(
        [{'post': 1167, 'action': 'edit', 'old': 'gov', 'new': 'x'}]
    )
    world[0].sets = [PACK]
    world[0].posts_by_id[5] = _with_emoji(f'{FIRE} hi', CAT)
    editor, _ = _editor(tmp_path, world, 'dry')
    with caplog.at_level(logging.INFO, logger='userbot'):
        asyncio.run(editor.tick(NOW))
    body = _catalog(tmp_path)
    assert body['sets'] == [
        {'short_name': 'TyomaPack', 'title': 'Tyoma', 'count': 1}
    ]
    assert body['sets_read'] is True
    assert body['emoji'] == [
        {
            'id': '777',
            'fallback': BUNNY,
            'source': ['set'],
            'set': 'TyomaPack',
        },
        {
            'id': CAT,
            'fallback': FIRE,
            'source': ['channel', 'constants'],
            'type': 'love',
        },
    ]
    assert 'emoji catalog: 1 sets, 2 emoji' in caplog.text
    assert 'emoji set TyomaPack "Tyoma": 1' in caplog.text
    assert editor.known == {'777', CAT}


def test_an_emoji_from_an_installed_set_goes_out_with_its_edit(
    tmp_path: Path,
) -> None:
    """On: the edit carries the added emoji to the account's write."""
    new = f'blog {_tag("777", BUNNY)}'
    world = _world(
        [{'post': 1167, 'action': 'edit', 'old': 'gov', 'new': new}]
    )
    world[0].sets = [PACK]
    editor, _ = _editor(tmp_path, world, 'on')
    asyncio.run(editor.tick(NOW))
    edit = world[0].edits[0]
    assert edit.text == f'it is blog {BUNNY} and blog.'
    assert edit.emoji == ((len('it is blog '), 2, '777'),)


def test_a_new_pin_is_sent_with_its_premium_emoji(tmp_path: Path) -> None:
    """The tags leave as spans over plain text, never as markup."""
    world = _world(
        [{'post': 0, 'action': 'pin_new', 'new': f'hi {_tag(CAT)}'}]
    )
    editor, _ = _editor(tmp_path, world, 'on')
    asyncio.run(editor.tick(NOW))
    sent = world[0].sent[0]
    assert sent.body == f'hi {FIRE}'
    assert [(s.kind, s.at, s.length, s.ref) for s in sent.spans] == [
        ('emoji', 3, 1, CAT)
    ]


def test_unreadable_sets_keep_the_constants_and_retry(tmp_path: Path) -> None:
    """A set's id is unknown until the sets read; the next tick retries."""
    new = _tag('777', BUNNY)
    world = _world(
        [{'post': 1167, 'action': 'edit', 'old': 'gov', 'new': new}]
    )
    world[0].sets = None
    editor, _ = _editor(tmp_path, world, 'dry')
    asyncio.run(editor.tick(NOW))
    assert _catalog(tmp_path)['sets_read'] is False
    assert editor.known == {CAT}
    reasons = [row['reason'] for row in _journal(tmp_path, 'outcome')]
    assert reasons == [tgedit.UNKNOWN_EMOJI]
    world[0].sets = [PACK]
    asyncio.run(editor.tick(NOW + 60))
    assert '777' in editor.known
    assert editor.catalog_at == NOW + 60


def test_no_installed_sets_is_said_in_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An account without sets says so: the question is then for a human."""
    editor, _ = _editor(tmp_path, _world(), 'dry')
    with caplog.at_level(logging.INFO, logger='userbot'):
        asyncio.run(editor.tick(NOW))
    assert 'no custom emoji sets installed' in caplog.text
    assert asyncio.run(editor.tick(NOW + 60)) == 0
    assert editor.catalog_at == NOW


def test_the_shipped_constants_are_the_first_catalog() -> None:
    """Every premium emoji of the constants may be used in a package."""
    params = worker.load_edit_params(config.read_json(config.CONSTANTS_PATH))
    assert len(params.emoji) == 39  # noqa: PLR2004 -- the shipped catalog
    assert {ident for _, ident, _ in params.emoji} == params.guard.emoji
    assert CAT in params.guard.emoji


def test_the_dry_check_matches_a_package_with_a_premium_emoji(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A tag with a constants id reads as matched; an unknown one does not."""
    labels = worker.load_edit_params(
        config.read_json(config.CONSTANTS_PATH)
    ).labels
    edits = [
        {'post': 1, 'action': 'edit', 'old': 'a', 'new': f'a {_tag(CAT)}'},
        {'post': 2, 'action': 'edit', 'old': 'b', 'new': _tag(STRANGER)},
    ]
    feed = tmp_path / 'tgedit.json'
    feed.write_text(_feed(_pkg(edits)), 'utf-8')
    snapshot = tmp_path / 'telegram.json'
    snapshot.write_text(_snapshot(_snap(1, 'a'), _snap(2, 'b')), 'utf-8')
    assert tgedit_check.main([str(feed), str(snapshot)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1] == f'{labels["post"]} 1 . edit -> {labels["dry_ok"]}'
    assert out[2].endswith(f'({labels["unknown_emoji"]})')
