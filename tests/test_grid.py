# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The grid keeper: a network that came out after the post joins its grid.

The posts are drawn by the aggregator's own ``compose`` and read back
through the adapter (``_linked``), so the planner reads exactly the grid
the poster writes; a completed post must equal the post ``compose`` would
have drawn had every network been there from the start.
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast

from minion_core import richtext
from minion_core.adapters import feed as feeds
from minion_core.adapters import userchat
from minions.userbot.core import config
from minions.userbot.core import gridfill
from minions.userbot.core import relink
from minions.userbot.core.humanize import Variety
from minions.userbot.core.models import Group
from minions.userbot.core.models import Item
from minions.userbot.core.render import compose
from minions.userbot.core.shortlinks import short_url
from minions.userbot.glue import grid as worker
from minions.userbot.glue.tgedit import to_post
from tests.conftest import install_telethon_stub

if TYPE_CHECKING:
    from minions.userbot.engines.premium_emoji import PremiumMessage

CONSTANTS = (
    Path(__file__).resolve().parent.parent
    / 'minions'
    / 'userbot'
    / 'aggregator_constants.json'
)
SETTINGS = config.read_json(CONSTANTS)
CONSTS = config.load_constants(CONSTANTS)
PARAMS = worker.load_grid_params(SETTINGS)
LAYOUT = PARAMS.layout
ORDER = ('tiktok', 'youtube', 'instagram', 'pinterest')
SOURCES = {
    'tiktok': 'https://www.tiktok.com/@da_eto_tyoma/video/7441234567890123456',
    'youtube': 'https://www.youtube.com/shorts/x3kFQjmf_BU',
    'instagram': 'https://www.instagram.com/reel/DCabcdefghi/',
    'pinterest': 'https://www.pinterest.com/pin/1234567890123/',
}
SHORT = {k: short_url(v, LAYOUT.domain) for k, v in SOURCES.items()}
CHANNEL = -100
NOW = 1_800_000_000.0
HOUR = 3600.0
OTHER_EMOJI = 42
"""A premium emoji the constants do not carry."""


# ------------------------------------------------------------ helpers


@dataclass
class _Glyph:
    """A vendor custom-emoji entity."""

    offset: int
    length: int
    document_id: int


@dataclass
class _TextUrl:
    """A vendor hidden link."""

    offset: int
    length: int
    url: str


@dataclass
class _Underline:
    """A vendor underline."""

    offset: int
    length: int


@dataclass
class _Message:
    """A vendor message: text and entities, no media."""

    id: int
    message: str
    entities: list[object]
    date: object = None
    media: object = None


def _units(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2


def _message(networks: tuple[str, ...], seed: int = 7) -> PremiumMessage:
    """Draw the post the aggregator sends for a video on ``networks``."""
    group = Group(title='Sketch title')
    for number, key in enumerate(networks):
        group.items[key] = Item(
            key=key,
            platform=key,
            title='Sketch title',
            url=SOURCES[key],
            thumbnail='',
            duration='0:0:20',
            msg_id=number,
        )
    return compose(
        group,
        ORDER,
        CONSTS,
        Variety(random.Random(seed)),
        announce='hello',
    )


def _vendor(message: PremiumMessage, post_id: int = 1175) -> _Message:
    """Turn a composed message into the vendor message Telegram stores."""
    text = message.text
    ents: list[object] = []
    for span in message.spans:
        offset = _units(text[: span.at])
        length = _units(text[span.at : span.at + span.length])
        if span.kind == richtext.EMOJI:
            ents.append(_Glyph(offset, length, int(span.ref)))
        elif span.kind == richtext.LINK:
            ents.append(_TextUrl(offset, length, span.ref))
        else:
            ents.append(_Underline(offset, length))
    return _Message(post_id, text, ents)


def _read(networks: tuple[str, ...], post_id: int = 1175) -> userchat.Linked:
    """Read the post back as the adapter reads it from the channel."""
    return userchat._linked(_vendor(_message(networks), post_id))


def _shape(ents: list[object]) -> list[tuple[str, int, int, object]]:
    """Each entity as (kind, offset, length, id or address), by offset."""
    out = []
    for one in ents:
        ident = getattr(one, 'document_id', None)
        url = getattr(one, 'url', None)
        kind, ref = (
            ('emoji', int(ident))
            if ident is not None
            else ('link', url)
            if url
            else ('other', '')
        )
        out.append((kind, one.offset, one.length, ref))  # type: ignore[attr-defined]
    return sorted(out, key=lambda row: (row[1], row[0]))


def _feed(*sketches: dict[str, str]) -> str:
    """Return the site's posts feed with these sketches."""
    posts = [{'id': f's{n}', 'links': s} for n, s in enumerate(sketches)]
    return json.dumps({'generated_at': 'x', 'posts': posts})


def _plan(post: userchat.Linked, *sketches: dict[str, str]) -> gridfill.Plan:
    """Plan one post against a feed of ``sketches``."""
    feed = gridfill.parse_feed(_feed(*sketches))
    return gridfill.plan(to_post(post), feed, LAYOUT)


def _apply(post: userchat.Linked, plan: gridfill.Plan) -> list[object]:
    """Return the entities the adapter would send for ``plan``."""
    install_telethon_stub()
    edit = userchat.Relinked(
        plan.text, plan.spans, emoji=plan.emoji, clones=plan.clones
    )
    return userchat._reentities(post, edit)


# ------------------------------------------------------------ planner


def test_a_late_network_completes_the_grid_as_compose_draws_it() -> None:
    """Posted with TikTok alone, then three networks came: the very post."""
    post = _read(('tiktok',))
    got = _plan(post, SHORT)
    assert got.added == ('youtube', 'instagram', 'pinterest')
    full = _vendor(_message(ORDER))
    assert got.text == full.message
    assert _shape(_apply(post, got)) == _shape(full.entities)


def test_a_missing_first_cell_reflows_the_rows() -> None:
    """TikTok came last (posts 1157, 1135, 1133): it opens the first row."""
    post = _read(('youtube', 'instagram', 'pinterest'))
    got = _plan(post, SHORT)
    assert got.added == ('tiktok',)
    full = _vendor(_message(ORDER))
    assert got.text == full.message
    assert _shape(_apply(post, got)) == _shape(full.entities)


def test_the_old_cells_keep_their_own_emoji_ids() -> None:
    """A cell drawn with another emoji keeps it; only the new one is ours."""
    raw = _vendor(_message(('tiktok',)))
    glyph = next(e for e in raw.entities if isinstance(e, _Glyph))
    raw.entities = [
        _Glyph(e.offset, e.length, OTHER_EMOJI)
        if isinstance(e, _Glyph) and e.offset == glyph.offset + 0
        else e
        for e in raw.entities
    ]
    post = userchat._linked(raw)
    ents = _apply(
        post,
        _plan(post, {'tiktok': SHORT['tiktok'], 'youtube': SHORT['youtube']}),
    )
    ids = [int(e.document_id) for e in ents if hasattr(e, 'document_id')]  # type: ignore[attr-defined]
    youtube_id = int(LAYOUT.emoji['youtube'][0])
    assert OTHER_EMOJI in ids
    assert youtube_id in ids


def test_text_after_the_grid_stays_and_moves() -> None:
    """The footer line under the grid (post 1175) keeps its link."""
    post = _read(('tiktok',))
    got = _plan(post, SHORT)
    footer = CONSTS.footer.label
    assert got.text.endswith(footer)
    tail = [
        e
        for e in _apply(post, got)
        if getattr(e, 'url', '') == CONSTS.footer.url
    ]
    assert len(tail) == 1
    start = _units(got.text) - _units(footer)
    assert (tail[0].offset, tail[0].length) == (start, _units(footer))  # type: ignore[attr-defined]


def test_a_complete_post_plans_nothing() -> None:
    """Every network of the feed is in the post: no edit, twice over."""
    post = _read(ORDER)
    got = _plan(post, SHORT)
    assert (got.added, got.reason) == ((), gridfill.COMPLETE)


def test_a_cell_the_feed_lacks_is_kept() -> None:
    """The feed lost a network the post has (post 1157's pin): kept."""
    post = _read(('youtube', 'pinterest'))
    got = _plan(post, {'youtube': SHORT['youtube'], 'tiktok': SHORT['tiktok']})
    assert got.added == ('tiktok',)
    assert SHORT['pinterest'] in {
        getattr(e, 'url', '') for e in _apply(post, got)
    }


def test_a_post_of_no_sketch_in_the_feed_is_left() -> None:
    """No cell address in the feed: not our video, nothing guessed."""
    post = _read(('tiktok',))
    got = _plan(post, {'youtube': SHORT['youtube']})
    assert got.reason == gridfill.NO_MATCH


def test_a_post_matching_two_sketches_is_left() -> None:
    """Two sketches claim the post's cells: ambiguous, skipped."""
    post = _read(('tiktok', 'youtube'))
    got = _plan(
        post, {'tiktok': SHORT['tiktok']}, {'youtube': SHORT['youtube']}
    )
    assert got.reason == gridfill.AMBIGUOUS


def test_a_post_without_a_grid_is_left() -> None:
    """A plain post (an announcement) has no cells: not a grid."""
    post = userchat._linked(_Message(9, 'just words', []))
    assert _plan(post, SHORT).reason == gridfill.NOT_A_GRID


def test_foreign_formatting_inside_the_grid_skips_the_post() -> None:
    """A bold over the grid would be cut by the reflow: skipped."""
    raw = _vendor(_message(('tiktok',)))
    first = min(e.offset for e in raw.entities if isinstance(e, _TextUrl))  # type: ignore[attr-defined]
    raw.entities.append(
        type(
            'MessageEntityBold',
            (),
            {'offset': first, 'length': 3, 'url': None},
        )()
    )
    post = userchat._linked(raw)
    assert _plan(post, SHORT).reason == gridfill.FORMATTING


def test_a_network_without_a_premium_emoji_is_not_added() -> None:
    """The channel carries no cell without its emoji."""
    post = _read(('tiktok',))
    layout = gridfill.Layout(
        LAYOUT.rows, LAYOUT.separator, LAYOUT.domain, {'tiktok': ('1', 'x')}
    )
    feed = gridfill.parse_feed(_feed(SHORT))
    got = gridfill.plan(to_post(post), feed, layout)
    assert got.reason == gridfill.NO_EMOJI


def test_a_broken_posts_feed_is_refused() -> None:
    """Not JSON or no posts list: the tick is skipped, not read as empty."""
    for text in ('<html>', '{"items": []}', '[]'):
        try:
            gridfill.parse_feed(text)
        except gridfill.FeedError:
            continue
        raise AssertionError(text)


def test_a_short_address_names_its_network() -> None:
    """Only the four video networks on the site's domain are cells."""
    assert gridfill.network_of(SHORT['tiktok'], 'daetotyoma.com') == 'tiktok'
    assert (
        gridfill.network_of('https://go.daetotyoma.com/tg', 'daetotyoma.com')
        == ''
    )
    assert gridfill.network_of(SOURCES['youtube'], 'daetotyoma.com') == ''


# ------------------------------------------------------------ the worker


@dataclass
class _Account:
    """A fake account: the channel's posts and every edit."""

    posts_by_id: dict[int, userchat.Linked]
    edits: list[tuple[int, userchat.Relinked]] = field(default_factory=list)
    refused: set[int] = field(default_factory=set)  # Telegram says no

    async def linked(self, chat: int, limit: int) -> list[userchat.Linked]:
        """Return the newest posts."""
        rows = sorted(self.posts_by_id.values(), key=lambda p: -p.id)
        return rows[:limit]

    async def posts(
        self, chat: int, ids: list[int]
    ) -> dict[int, userchat.Linked] | None:
        """Return the asked posts that exist."""
        return {i: self.posts_by_id[i] for i in ids if i in self.posts_by_id}

    async def relink(
        self, chat: int, post: userchat.Linked, edit: userchat.Relinked
    ) -> bool:
        """Record an edit and store the post as edited."""
        install_telethon_stub()
        self.edits.append((post.id, edit))
        if post.id in self.refused:
            return False
        ents = userchat._reentities(post, edit)
        self.posts_by_id[post.id] = userchat._linked(
            _Message(post.id, edit.text, ents, date=post.date)
        )
        return True


def _dated(post: userchat.Linked, date: float) -> userchat.Linked:
    """Return the post with another date."""
    return userchat.Linked(
        post.id,
        post.text,
        date,
        post.spans,
        post.links,
        raw=post.raw,
        kinds=post.kinds,
        refs=post.refs,
    )


@dataclass
class _Site:
    """The posts feed as the site serves it right now."""

    body: str = ''

    async def read(self) -> str:
        if not self.body:
            msg = 'posts.json: HTTPError'
            raise feeds.FeedUnreachableError(msg)
        return self.body


def _keeper(
    account: _Account, site: _Site, tmp_path: Path, **over: object
) -> worker.GridKeeper:
    """Build a keeper in ``on`` mode, always awake, with no real sleeps."""

    async def nap(_: float) -> None:
        return None

    settings: dict[str, object] = {'mode': 'on', **over}
    params = replace(PARAMS, clock=relink.Clock(), **settings)  # type: ignore[arg-type]
    deps = worker.GridDeps(
        account=cast('userchat.Account', account),
        channel=lambda: CHANNEL,
        feed=site.read,
        journal=tmp_path / worker.JOURNAL_FILE,
        params=params,
        sleep=nap,
    )
    return worker.GridKeeper(deps)


def test_a_network_out_after_the_post_lands_within_two_hours(
    tmp_path: Path,
) -> None:
    """Posted with TikTok; YouTube shows in the feed an hour later.

    The tick after it adds the cell -- the tick period is what bounds the
    wait, and it is well under two hours -- and a later tick edits nothing.
    """
    assert PARAMS.tick_sec <= 2 * HOUR
    assert PARAMS.mode == 'on'
    post = _dated(_read(('tiktok',)), NOW)
    account = _Account({post.id: post})
    site = _Site(_feed({'tiktok': SHORT['tiktok']}))
    keeper = _keeper(account, site, tmp_path)
    assert asyncio.run(keeper.tick(NOW + PARAMS.tick_sec)) == []
    site.body = _feed({'tiktok': SHORT['tiktok'], 'youtube': SHORT['youtube']})
    landed_at = NOW + HOUR + PARAMS.tick_sec
    plans = asyncio.run(keeper.tick(landed_at))
    assert [p.added for p in plans] == [('youtube',)]
    assert landed_at - (NOW + HOUR) <= 2 * HOUR
    urls = {one.url for one in account.posts_by_id[post.id].links}
    assert SHORT['youtube'] in urls
    assert asyncio.run(keeper.tick(landed_at + PARAMS.tick_sec)) == []
    assert len(account.edits) == 1


def test_the_journal_keeps_the_post_before_the_edit(tmp_path: Path) -> None:
    """A before-image, then the outcome: enough to undo by hand."""
    post = _dated(_read(('tiktok',)), NOW)
    account = _Account({post.id: post})
    keeper = _keeper(account, _Site(_feed(SHORT)), tmp_path)
    asyncio.run(keeper.tick(NOW))
    rows = [
        json.loads(line)
        for line in (tmp_path / worker.JOURNAL_FILE).read_text().splitlines()
    ]
    assert [r['what'] for r in rows] == ['post', 'outcome']
    assert rows[0]['text'] == post.text
    assert rows[1]['status'] == 'applied'


def test_a_post_older_than_the_window_waits_unless_named(
    tmp_path: Path,
) -> None:
    """A month-old post is left; named in ``posts`` it is caught up."""
    old = _dated(_read(('youtube',), post_id=1098), NOW - 30 * 24 * HOUR)
    account = _Account({old.id: old})
    site = _Site(_feed(SHORT))
    unnamed = _keeper(account, site, tmp_path, posts=())
    assert asyncio.run(unnamed.tick(NOW)) == []
    named = _keeper(account, site, tmp_path, posts=(1098,))
    assert [p.post_id for p in asyncio.run(named.tick(NOW))] == [1098]


def test_dry_mode_edits_nothing(tmp_path: Path) -> None:
    """``dry`` plans and logs, the channel stays as it is."""
    post = _dated(_read(('tiktok',)), NOW)
    account = _Account({post.id: post})
    keeper = _keeper(account, _Site(_feed(SHORT)), tmp_path, mode='dry')
    assert len(asyncio.run(keeper.tick(NOW))) == 1
    assert account.edits == []


def test_an_unreachable_feed_skips_the_tick(tmp_path: Path) -> None:
    """No feed is not "nothing to add": the tick does nothing at all."""
    post = _dated(_read(('tiktok',)), NOW)
    account = _Account({post.id: post})
    assert asyncio.run(_keeper(account, _Site(), tmp_path).tick(NOW)) == []
    assert account.edits == []


def test_at_most_per_tick_posts_are_edited(tmp_path: Path) -> None:
    """The rest waits for the next tick."""
    posts = {i: _dated(_read(('tiktok',), post_id=i), NOW) for i in (1, 2, 3)}
    account = _Account(posts)
    cap = len(posts) - 1
    keeper = _keeper(account, _Site(_feed(SHORT)), tmp_path, per_tick=cap)
    assert len(asyncio.run(keeper.tick(NOW))) == len(posts)
    assert len(account.edits) == cap


def test_a_failed_edit_is_not_retried_while_the_plan_is_the_same(
    tmp_path: Path,
) -> None:
    """Telegram refused the edit: the next tick does not try it again."""
    post = _dated(_read(('tiktok',)), NOW)
    account = _Account({post.id: post}, refused={post.id})
    keeper = _keeper(account, _Site(_feed(SHORT)), tmp_path)
    asyncio.run(keeper.tick(NOW))
    assert asyncio.run(keeper.tick(NOW + PARAMS.tick_sec)) == []
    assert len(account.edits) == 1


def test_failed_posts_do_not_take_the_slots_of_the_rest(
    tmp_path: Path,
) -> None:
    """Two posts that always fail; the third one is edited on the next tick."""
    posts = {i: _dated(_read(('tiktok',), post_id=i), NOW) for i in (1, 2, 3)}
    account = _Account(posts, refused={2, 3})
    keeper = _keeper(account, _Site(_feed(SHORT)), tmp_path, per_tick=2)
    asyncio.run(keeper.tick(NOW))
    asyncio.run(keeper.tick(NOW + PARAMS.tick_sec))
    assert [i for i, _ in account.edits] == [3, 2, 1]
    assert SHORT['youtube'] in {
        one.url for one in account.posts_by_id[1].links
    }


def test_a_restart_remembers_the_failures_from_the_journal(
    tmp_path: Path,
) -> None:
    """A new keeper (container restart) reads the failures back."""
    post = _dated(_read(('tiktok',)), NOW)
    account = _Account({post.id: post}, refused={post.id})
    site = _Site(_feed(SHORT))
    asyncio.run(_keeper(account, site, tmp_path).tick(NOW))
    restarted = _keeper(account, site, tmp_path)
    assert asyncio.run(restarted.tick(NOW + PARAMS.tick_sec)) == []
    assert len(account.edits) == 1


def test_a_failed_post_is_tried_again_once_the_feed_changes(
    tmp_path: Path,
) -> None:
    """Another network in the feed is another plan: one more try."""
    post = _dated(_read(('tiktok',)), NOW)
    account = _Account({post.id: post}, refused={post.id})
    site = _Site(
        _feed({'tiktok': SHORT['tiktok'], 'youtube': SHORT['youtube']})
    )
    keeper = _keeper(account, site, tmp_path)
    asyncio.run(keeper.tick(NOW))
    site.body = _feed(SHORT)
    account.refused.clear()
    plans = asyncio.run(keeper.tick(NOW + PARAMS.tick_sec))
    assert [i for i, _ in account.edits] == [post.id, post.id]
    assert len(plans) == 1
    assert 'youtube' in plans[0].added
