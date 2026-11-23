# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The link keeper: what it rewrites, what it leaves, how it edits.

Pure logic (core/relink) and the worker over a fake account (glue/links):
dry mode edits nothing and reports once, on mode edits within the daily
cap, stops on a strained lane, and a second pass changes nothing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import cast

from minion_core.adapters import userchat
from minions.userbot.core import relink
from minions.userbot.core.shortlinks import short_url
from minions.userbot.glue import links

if TYPE_CHECKING:
    from pathlib import Path

DOMAIN = 'daetotyoma.com'
TIKTOK = 'https://www.tiktok.com/@da_eto_tyoma/video/7709914477419924758?x=1'
YOUTUBE = 'https://www.youtube.com/watch?v=9eZ2CS-4Q84'
CHANNEL = -100
NOW = 1_800_000_000.0
HOUR = 3600.0
CAP = 2
THREE = 3
LIMIT = 1000


def _hidden(index: int, url: str) -> relink.Link:
    """Return a text_url link at a fixed place."""
    return relink.Link(relink.HIDDEN, index * 10, 4, url)


# ----------------------------------------------------------- what changes


def test_a_raw_video_link_becomes_the_short_one() -> None:
    """TikTok and YouTube videos get the shortener's own address."""
    got = relink.plan(1, (_hidden(0, TIKTOK), _hidden(1, YOUTUBE)), DOMAIN)
    assert [c.new for c in got.changes] == [
        short_url(TIKTOK, DOMAIN),
        short_url(YOUTUBE, DOMAIN),
    ]
    assert got.changes[0].new.startswith('https://tiktok.daetotyoma.com/')


def test_our_own_and_telegram_links_are_never_touched() -> None:
    """The site, its subdomains and t.me are neither changed nor listed."""
    kept = (
        _hidden(0, 'https://tiktok.daetotyoma.com/abcdefg'),
        _hidden(1, 'https://daetotyoma.com/?s=telegram'),
        _hidden(2, 'https://t.me/da_eto_tyoma'),
    )
    assert relink.plan(1, kept, DOMAIN) == relink.Plan(1)


def test_other_links_are_listed_not_changed() -> None:
    """A profile or a shop link is reported back, untouched."""
    other = 'https://www.amazon.de/hz/wishlist/ls/X'
    got = relink.plan(1, (_hidden(0, other),), DOMAIN)
    assert got.changes == ()
    assert got.others == (other,)


def test_a_rewritten_post_plans_nothing_the_second_time() -> None:
    """Idempotence: the short address is ours, so a second pass is empty."""
    first = relink.plan(1, (_hidden(0, TIKTOK),), DOMAIN)
    again = relink.plan(1, (_hidden(0, first.changes[0].new),), DOMAIN)
    assert again == relink.Plan(1)


def test_no_domain_means_no_rewrite() -> None:
    """A blank domain switches the keeper off entirely."""
    assert relink.rewrite(TIKTOK, '') == ''


# ------------------------------------------------------------- the edit


def test_a_hidden_link_keeps_text_and_every_span() -> None:
    """Only the address changes: the text and offsets stay as they were."""
    text = 'see \U0001f929 watch'
    spans = ((4, 2), (7, 5))
    post = relink.Post(
        text, spans, (relink.Link(relink.HIDDEN, 7, 5, TIKTOK),)
    )
    got = relink.apply(post, relink.plan(1, post.links, DOMAIN).changes)
    assert got == relink.Edit(text, spans)


def test_a_visible_link_shifts_what_follows_and_wraps_it() -> None:
    """A visible url is replaced; later spans move, a wrapping one grows."""
    head = 'a \U0001f929 '
    text = f'{head}{YOUTUBE} end'
    at = len(head.encode('utf-16-le')) // 2
    size = len(YOUTUBE)
    after = at + size + 1
    spans = ((2, 2), (at, size), (0, at + size), (after, 3))
    link = relink.Link(relink.VISIBLE, at, size, YOUTUBE)
    post = relink.Post(text, spans, (link,))
    got = relink.apply(post, relink.plan(1, (link,), DOMAIN).changes)
    new = short_url(YOUTUBE, DOMAIN)
    delta = len(new) - size
    assert got.text == f'{head}{new} end'
    assert got.spans == (
        (2, 2),
        (at, len(new)),
        (0, at + len(new)),
        (after + delta, 3),
    )


def test_a_visible_link_without_a_scheme_is_still_a_video() -> None:
    """Telegram shows ``youtube.com/watch?...`` bare; it still counts."""
    bare = YOUTUBE.removeprefix('https://')
    assert relink.rewrite(bare, DOMAIN) == short_url(YOUTUBE, DOMAIN)


def test_the_dry_report_names_old_and_new() -> None:
    """The operator sees ``post N: old -> new`` per change."""
    one = relink.plan(7, (_hidden(0, YOUTUBE),), DOMAIN)
    assert relink.report([one]) == [
        f'post 7: {YOUTUBE} -> {short_url(YOUTUBE, DOMAIN)}'
    ]


# ------------------------------------------------------- clock and budget


def test_the_waking_window_and_a_wrapping_one() -> None:
    """[5, 23) is awake at noon and asleep at 2am; [22, 6) wraps midnight."""
    clock = relink.Clock(0, 5, 23)
    noon = 1_800_014_400.0  # 12:00 UTC
    assert clock.awake(noon)
    assert not clock.awake(noon - 10 * HOUR)
    night = relink.Clock(0, 22, 6)
    assert night.awake(noon - 10 * HOUR)
    assert not night.awake(noon)


def test_the_budget_resets_on_a_new_day() -> None:
    """Edits count per local day."""
    spent = relink.Budget().spend('d1').spend('d1')
    assert spent.left('d1', CAP) == 0
    assert spent.left('d2', CAP) == CAP


def test_a_fresh_post_waits() -> None:
    """A post younger than the delay is not touched yet."""
    assert not relink.due(NOW - 60, NOW, 1800)
    assert relink.due(NOW - 1800, NOW, 1800)


# ---------------------------------------------------------- the worker


def _post(msg_id: int, url: str = TIKTOK) -> userchat.Linked:
    """Return a post with one hidden link, two hours before NOW."""
    return userchat.Linked(
        id=msg_id,
        text='watch',
        date=NOW - 2 * HOUR,
        spans=((0, 5),),
        links=(userchat.LinkSpan(0, 'text', 0, 5, url),),
    )


@dataclass
class _Fake:
    """The two account calls the keeper makes, recorded."""

    posts: list[userchat.Linked]
    edits: list[tuple[int, dict[int, str]]] = field(default_factory=list)
    flooded: bool = False

    async def linked(
        self, chat: int, limit: int, below: int = 0
    ) -> list[userchat.Linked]:
        """Return the posts below ``below``, newest first."""
        rows = [p for p in self.posts if not below or p.id < below]
        return sorted(rows, key=lambda p: -p.id)[:limit]

    async def relink(
        self, chat: int, post: userchat.Linked, edit: userchat.Relinked
    ) -> bool:
        """Record the edit and apply it to the fake channel."""
        self.edits.append((post.id, edit.urls))
        fixed = [
            userchat.LinkSpan(
                one.index,
                one.kind,
                one.offset,
                one.length,
                edit.urls.get(one.index, one.url),
            )
            for one in post.links
        ]
        self.posts = [
            p if p.id != post.id else _replace_links(p, fixed)
            for p in self.posts
        ]
        return True

    def strained(self, kind: str) -> bool:
        """Report the write lane as flooded when asked to."""
        return self.flooded


def _replace_links(
    post: userchat.Linked, fixed: list[userchat.LinkSpan]
) -> userchat.Linked:
    """Return ``post`` with its links swapped."""
    return userchat.Linked(
        post.id, post.text, post.date, post.spans, tuple(fixed)
    )


def _keeper(tmp_path: Path, fake: _Fake, said: list[str]) -> links.LinkKeeper:
    """Build a keeper over the fake account, awake all day, no pauses."""

    async def say(text: str) -> None:
        said.append(text)

    params = links.LinkParams(
        domain=DOMAIN,
        cap_per_day=CAP,
        delay_sec=1800,
        page=THREE,
        pause_sec=0,
    )
    keeper = links.LinkKeeper(
        links.LinkDeps(
            account=cast('userchat.Account', fake),
            channel=lambda: CHANNEL,
            say=say,
            path=tmp_path / links.STATE_FILE,
            params=params,
        )
    )
    keeper.load()
    return keeper


def test_dry_is_the_default_and_edits_nothing(tmp_path: Path) -> None:
    """Fresh state is dry: a report goes out, no post is edited."""
    fake, said = _Fake([_post(1), _post(2)]), []
    keeper = _keeper(tmp_path, fake, said)
    assert keeper.state.mode == 'dry'
    assert asyncio.run(keeper.tick(NOW)) == 0
    assert fake.edits == []
    assert 'post 2:' in said[0]
    assert 'post 1:' in said[0]


def test_dry_reports_each_post_once(tmp_path: Path) -> None:
    """The newest page is re-read every tick but reported once."""
    fake, said = _Fake([_post(1)]), []
    keeper = _keeper(tmp_path, fake, said)
    asyncio.run(keeper.tick(NOW))
    asyncio.run(keeper.tick(NOW))
    assert len(said) == 1


def test_on_edits_within_the_cap_then_resumes(tmp_path: Path) -> None:
    """On edits at most cap a day; the history cursor survives a restart."""
    fake = _Fake([_post(n) for n in range(1, 6)])
    keeper = _keeper(tmp_path, fake, [])
    asyncio.run(keeper.command('/links_on'))
    assert asyncio.run(keeper.tick(NOW)) == CAP
    assert asyncio.run(keeper.tick(NOW)) == 0  # cap reached today
    again = _keeper(tmp_path, fake, [])
    assert again.state.mode == 'on'
    tomorrow = NOW + 24 * HOUR
    edited = asyncio.run(again.tick(tomorrow))
    edited += asyncio.run(again.tick(tomorrow + 24 * HOUR))
    edited += asyncio.run(again.tick(tomorrow + 48 * HOUR))
    assert edited == THREE
    assert sorted(p for p, _ in fake.edits) == [1, 2, 3, 4, 5]


def test_a_second_pass_changes_nothing(tmp_path: Path) -> None:
    """Once every link is short, the keeper edits nothing more."""
    fake = _Fake([_post(1)])
    keeper = _keeper(tmp_path, fake, [])
    asyncio.run(keeper.command('/links_on'))
    asyncio.run(keeper.tick(NOW))
    asyncio.run(keeper.command('/links_dry'))
    asyncio.run(keeper.command('/links_on'))
    assert asyncio.run(keeper.tick(NOW + 24 * HOUR)) == 0
    assert len(fake.edits) == 1


def test_a_flood_stops_the_pass(tmp_path: Path) -> None:
    """A strained write lane means no edit goes out this tick."""
    fake = _Fake([_post(1)], flooded=True)
    keeper = _keeper(tmp_path, fake, [])
    asyncio.run(keeper.command('/links_on'))
    assert asyncio.run(keeper.tick(NOW)) == 0
    assert fake.edits == []


def test_a_fresh_post_is_left_for_later(tmp_path: Path) -> None:
    """A post inside the delay is not edited yet."""
    young = userchat.Linked(
        9,
        'x',
        NOW - 60,
        ((0, 1),),
        (userchat.LinkSpan(0, 'text', 0, 1, TIKTOK),),
    )
    fake = _Fake([young])
    keeper = _keeper(tmp_path, fake, [])
    asyncio.run(keeper.command('/links_on'))
    assert asyncio.run(keeper.tick(NOW)) == 0


def test_off_does_nothing_and_status_answers(tmp_path: Path) -> None:
    """Off reads nothing; /links reports the mode."""
    fake, said = _Fake([_post(1)]), []
    keeper = _keeper(tmp_path, fake, said)
    asyncio.run(keeper.command('/links_off'))
    assert asyncio.run(keeper.tick(NOW)) == 0
    assert said[-1].startswith('links: off')
    assert not asyncio.run(keeper.command('/status'))


def test_report_chunks_stay_under_the_limit() -> None:
    """A long dry report is split under Telegram's message size."""
    lines = ['x' * 100] * 100
    packed = links.chunks(lines, LIMIT)
    assert all(len(one) <= LIMIT for one in packed)
    assert sum(one.count('x' * 100) for one in packed) == len(lines)


# ------------------------------------------------------------ the adapter


class MessageEntityUrl:
    """Stand-in for the vendor's visible-url entity (matched by name)."""

    def __init__(self, offset: int, length: int) -> None:
        """Place the entity."""
        self.offset = offset
        self.length = length


class MessageEntityBold:
    """Stand-in for a formatting entity the keeper must carry over."""

    def __init__(self, offset: int, length: int) -> None:
        """Place the entity."""
        self.offset = offset
        self.length = length


class MessageEntityTextUrl(MessageEntityBold):
    """Stand-in for the vendor's hidden-link entity."""

    def __init__(self, offset: int, length: int, url: str) -> None:
        """Place the entity and its address."""
        super().__init__(offset, length)
        self.url = url


def test_the_adapter_reads_every_entity_and_copies_them_back() -> None:
    """Bold survives the edit; the hidden link changes only its address."""
    text = f'go {YOUTUBE} now'
    raw = type(
        'Raw',
        (),
        {
            'id': 5,
            'message': text,
            'date': NOW,
            'media': None,
            'entities': [
                MessageEntityBold(0, 2),
                MessageEntityUrl(3, len(YOUTUBE)),
                MessageEntityTextUrl(len(text) - 3, 3, TIKTOK),
            ],
        },
    )()
    post = userchat._linked(raw)
    assert [(one.kind, one.url) for one in post.links] == [
        ('url', YOUTUBE),
        ('text', TIKTOK),
    ]
    edit = links.to_edit(
        post, relink.plan(5, links.to_post(post).links, DOMAIN)
    )
    ents = userchat._reentities(post, edit)
    bold, url, hidden = ents
    assert type(bold).__name__ == 'MessageEntityBold'
    assert (bold.offset, bold.length) == (0, 2)  # type: ignore[attr-defined]
    assert url.length == len(short_url(YOUTUBE, DOMAIN))  # type: ignore[attr-defined]
    assert hidden.url == short_url(TIKTOK, DOMAIN)  # type: ignore[attr-defined]
    assert raw.entities[2].url == TIKTOK  # the original is not mutated
    assert edit.text == f'go {short_url(YOUTUBE, DOMAIN)} now'
