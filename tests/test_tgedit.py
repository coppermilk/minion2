# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The channel editor: parse, approve, dry-run, limits, never-touch, apply.

Pure logic (core/tgedit) and the worker over a fake account and a fake
board (glue/tgedit). The dry run is the heart of it: ``old`` that matches
the post's current text is applied, anything else is skipped and said so.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import cast

import pytest

from minion_core.adapters import board as boardapi
from minion_core.adapters import userchat
from minions.userbot.core import config
from minions.userbot.core import relink
from minions.userbot.core import tgedit
from minions.userbot.glue import tgedit as worker

if TYPE_CHECKING:
    from pathlib import Path

CHAT = 't.me/da_eto_tyoma_chat'
WATCH = 'Watch'
GUARD = tgedit.Guard(urls=(CHAT,), link_texts=(WATCH,))
WORD = 'ok'
APPROVAL = tgedit.Approval(WORD, ('strateg',))
AUTHOR = 'audience-head'
CHANNEL = -100
NOW = 1_800_000_000.0
PACKAGE = 475
PAUSE = 10.0
EMPTY = tgedit.Outcome(tgedit.Item(1, tgedit.EDIT, 'x'), tgedit.DRY_OK)


def _block(items: list[dict[str, object]]) -> str:
    """Return a package note's text with its JSON block."""
    return f'package\n```json\n{json.dumps(items)}\n```'


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
    items = tgedit.parse(
        _block(
            [
                {'post': 0, 'action': 'desc', 'new': '\nfor brands'},
                {'post': 0, 'action': 'pin_new', 'new': 'hello'},
                {'post': 1142, 'action': 'delete'},
                {'post': 1006, 'action': 'delete', 'only_if_empty': True},
                {'post': 944, 'action': 'edit', 'old': 'a', 'new': 'b'},
                {'post': 944, 'action': 'link', 'old': 'u', 'new': 'v'},
            ]
        )
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


@pytest.mark.parametrize(
    ('text', 'says'),
    [
        ('no block here', 'no ```json'),
        ('```json\n[{"post": 1,]\n```', 'does not parse'),
        ('```json\n[]\n```', 'non-empty list'),
        (_block([{'post': 1, 'action': 'move'}]), "action 'move'"),
        (_block([{'post': 1, 'action': 'delete', 'x': 1}]), 'unknown key'),
        (_block([{'post': 5, 'action': 'desc', 'new': 'x'}]), 'needs post 0'),
        (_block([{'post': 0, 'action': 'edit', 'old': 'x'}]), 'needs post >0'),
        (_block([{'post': 1, 'action': 'edit'}]), 'non-empty old/new'),
        (_block([{'post': -1, 'action': 'delete'}]), 'whole number'),
        (_block([{'post': True, 'action': 'delete'}]), 'whole number'),
    ],
)
def test_a_broken_package_is_refused_whole(text: str, says: str) -> None:
    """A typo stops the package before anything is touched."""
    with pytest.raises(tgedit.PackageError, match=None) as caught:
        tgedit.parse(text)
    assert says in str(caught.value)


def test_fifteen_deletions_pass_sixteen_refuse_the_package() -> None:
    """At most 15 deletions a package; the config may not raise that."""
    fifteen = tuple(tgedit.Item(n, tgedit.DELETE) for n in range(1, 16))
    tgedit.check_limits(fifteen, 15)
    sixteen = (*fifteen, tgedit.Item(99, tgedit.DELETE))
    with pytest.raises(tgedit.PackageError, match='16 deletions'):
        tgedit.check_limits(sixteen, 99)


# ------------------------------------------------------------- approval


def _note(ident: int, role: str, text: str) -> tgedit.Note:
    """Return a board note written after the package."""
    return tgedit.Note(ident, role, text, '2')


PKG = tgedit.Note(PACKAGE, AUTHOR, 'package', '1')


def test_the_strategist_approves_with_the_word_and_the_id() -> None:
    """An approver's ``<word> #<id>`` anywhere on the board approves."""
    notes = [_note(497, 'strateg', 'OK #475\nchecked it')]
    assert APPROVAL.by(PKG, notes) == 'strateg'


@pytest.mark.parametrize(
    'note',
    [
        _note(500, AUTHOR, 'ok #475'),  # the author cannot approve own work
        _note(501, 'astro-dev', 'ok #475'),  # not an approver
        _note(502, 'strateg', 'ok #4750'),  # another package
        tgedit.Note(503, 'strateg', 'ok #475', '0'),  # older than it
        _note(504, 'strateg', 'not yet #475'),  # no approval word
        _note(505, 'strateg', 'not ok #475'),  # the word, negated
        _note(506, 'strateg', 'I will say ok #475 later'),  # not a verdict
    ],
)
def test_no_approval_without_an_approver_saying_it(note: tgedit.Note) -> None:
    """Only a different, permitted role's explicit word approves."""
    assert APPROVAL.by(PKG, [note]) == ''


def test_the_approval_may_open_any_line_of_the_note() -> None:
    """A verdict on its own line after a preamble still approves."""
    notes = [_note(507, 'strateg', 'checked the package\n  ok #475')]
    assert APPROVAL.by(PKG, notes) == 'strateg'


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
        self.posts_by_id[900] = userchat.Linked(900, text.body, NOW, (), ())
        return 900

    async def pin(self, chat: int, msg_id: int) -> bool:
        """Record a pin."""
        self.writes.append(('pin', msg_id))
        return True


@dataclass
class _Board:
    """A fake board: the package, the approvals, and the replies."""

    notes_list: list[boardapi.BoardNote]
    replies: list[tuple[int, str, str]] = field(default_factory=list)

    def notes(self, **query: object) -> list[boardapi.BoardNote]:
        """Answer like the live board did on 26.11.

        ``q`` searches every note, replies and the archive included, in
        text and tags; without it the list holds only root notes, so a
        package replied in a thread is reached through ``q`` alone.
        """
        kind = query.get('kind')
        rows = [n for n in self.notes_list if not kind or n.kind == kind]
        if 'q' in query:
            q = str(query['q'])
            return [n for n in rows if q in n.text or q in n.tags]
        return [
            n
            for n in rows
            if query.get('tag') in n.tags
            and not n.parent_id
            and not n.archived
        ]

    def reply(self, parent: int, text: str, ref: str) -> int:
        """Record a reply."""
        self.replies.append((parent, text, ref))
        return len(self.replies)


THREAD = 388
"""The thread the live package #475 was replied into (task No 92)."""


def _row(ident: int, role: str, text: str) -> boardapi.BoardNote:
    """Return a reply in THREAD; a package (a JSON block) carries the tag."""
    tags = ('tg-edit',) if '```json' in text else ()
    return boardapi.BoardNote(
        ident,
        role,
        'note',
        text,
        tags,
        f'2026-11-{ident}',
        parent_id=THREAD,
    )


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


def _world(
    items: list[dict[str, object]] | None = None, *, approved: bool = True
) -> tuple[_Account, _Board]:
    """Return the account and the board of one approved package."""
    account = _Account(
        {
            1142: _linked(1142, 'old tour'),
            1167: _linked(1167, 'it is gov and blog.'),
            944: _linked(944, 'support'),
            1000: _linked(1000, 'not in the package'),
        }
    )
    notes = [_row(25, AUTHOR, _block(items or ITEMS))]
    if approved:
        notes.append(_row(26, 'strateg', 'ok #25'))
    return account, _Board(notes)


def _editor(
    tmp_path: Path, world: tuple[_Account, _Board], mode: str
) -> tuple[worker.ChannelEditor, list[float]]:
    """Build an editor in ``mode``; return it and the pauses it took."""
    account, board = world
    slept: list[float] = []

    async def sleep(sec: float) -> None:
        slept.append(sec)

    params = worker.EditParams(
        mode=mode,
        board_url='https://board',
        authors=(AUTHOR,),
        approval=APPROVAL,
        pause_sec=PAUSE,
        guard=GUARD,
    )
    editor = worker.ChannelEditor(
        worker.EditDeps(
            account=cast('userchat.Account', account),
            board=cast('boardapi.Board', board),
            channel=lambda: CHANNEL,
            path=tmp_path / worker.STATE_FILE,
            journal=tmp_path / worker.JOURNAL_FILE,
            params=params,
            sleep=sleep,
        )
    )
    editor.load()
    return editor, slept


def test_off_reads_nothing(tmp_path: Path) -> None:
    """The shipped mode touches neither the board nor the channel."""
    account, board = _world()
    editor, _ = _editor(tmp_path, (account, board), 'off')
    assert asyncio.run(editor.tick(NOW)) == 0
    assert board.replies == []
    assert account.writes == []


def test_dry_reports_into_the_thread_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """Dry: every item's dry-run outcome goes to the package's thread once."""
    account, board = _world()
    editor, _ = _editor(tmp_path, (account, board), 'dry')
    assert asyncio.run(editor.tick(NOW)) == 1
    assert account.writes == []
    parent, text, ref = board.replies[0]
    assert (parent, ref) == (25, 'tgedit-25-dry-1')
    assert 'post 1167 . edit -> dry_ok' in text
    assert 'post 944 . edit -> skipped (not_found)' in text
    assert asyncio.run(editor.tick(NOW)) == 0
    assert len(board.replies) == 1


def test_on_applies_journals_paces_and_reports(tmp_path: Path) -> None:
    """On: what passed is written, 10 s apart, after its before-image."""
    account, board = _world()
    editor, slept = _editor(tmp_path, (account, board), 'on')
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
    journal = (tmp_path / worker.JOURNAL_FILE).read_text('utf-8').splitlines()
    before = [json.loads(line) for line in journal]
    assert [row.get('text') for row in before if row['what'] == 'post'] == [
        'old tour',
        'it is gov and blog.',
    ]
    _, text, _ = board.replies[0]
    assert 'applied 4' in text
    assert 'skipped 1' in text
    assert asyncio.run(editor.tick(NOW)) == 0  # applied once, never twice


def test_an_unapproved_package_waits(tmp_path: Path) -> None:
    """No approval: nothing is read, written or reported."""
    account, board = _world(approved=False)
    editor, _ = _editor(tmp_path, (account, board), 'on')
    assert asyncio.run(editor.tick(NOW)) == 0
    assert account.writes == []


def test_only_live_tagged_packages_are_picked_up(tmp_path: Path) -> None:
    """A note that only names the tag, or an archived package, is no package.

    The real reply to the package (#476) says ``tg-edit`` in its text; the
    board's ``q`` finds it, the tag filter drops it.
    """
    account, board = _world()
    board.notes_list += [
        boardapi.BoardNote(
            24,
            AUTHOR,
            'note',
            'format: see tg-edit',
            (),
            '2026-11-24',
            parent_id=THREAD,
        ),
        dataclasses.replace(_row(23, AUTHOR, _block(ITEMS)), archived=True),
        _row(27, 'strateg', 'ok #23'),
    ]
    editor, _ = _editor(tmp_path, (account, board), 'dry')
    assert asyncio.run(editor.tick(NOW)) == 1
    assert [parent for parent, _, _ in board.replies] == [25]


def test_a_package_by_another_role_is_ignored(tmp_path: Path) -> None:
    """Only the configured authors write packages."""
    account, board = _world()
    board.notes_list[0] = _row(25, 'astro-dev', _block(ITEMS))
    editor, _ = _editor(tmp_path, (account, board), 'on')
    assert asyncio.run(editor.tick(NOW)) == 0


def test_too_many_deletions_refuse_the_package(tmp_path: Path) -> None:
    """Sixteen deletions: refused whole, reported, nothing deleted."""
    many: list[dict[str, object]] = [
        {'post': n, 'action': 'delete'} for n in range(1, 17)
    ]
    account, board = _world(many)
    editor, _ = _editor(tmp_path, (account, board), 'on')
    asyncio.run(editor.tick(NOW))
    assert account.writes == []
    assert '16 deletions' in board.replies[0][1]


def test_without_rights_every_item_says_so(tmp_path: Path) -> None:
    """An account that may not edit the channel edits nothing."""
    account, board = _world()
    account.granted = frozenset()
    editor, _ = _editor(tmp_path, (account, board), 'on')
    asyncio.run(editor.tick(NOW))
    assert account.writes == []
    assert board.replies[0][1].count('(no_rights)') == len(ITEMS)


def test_an_unreadable_channel_retries_later(tmp_path: Path) -> None:
    """A failed read is no 'post gone': the package stays pending."""
    account, board = _world()
    account.unreadable = True
    editor, _ = _editor(tmp_path, (account, board), 'on')
    with pytest.raises(worker.ChannelUnreadableError):
        asyncio.run(editor.tick(NOW))
    assert editor.state.done == set()
    account.unreadable = False
    assert asyncio.run(editor.tick(NOW)) == 1


# ------------------------------------------------------- configuration


def test_the_shipped_constants_keep_the_editor_off_and_safe() -> None:
    """Off by default; the chat and the watch links are protected."""
    params = worker.load_edit_params(config.read_json(config.CONSTANTS_PATH))
    assert params.mode == 'off'
    assert params.max_deletes == tgedit.MAX_DELETES
    assert params.pause_sec >= tgedit.MIN_PAUSE_SEC
    assert CHAT in params.guard.urls
    assert params.guard.link_texts
    assert params.approval.approvers == ('strateg',)
    assert params.approval.word


def test_the_hard_limits_cannot_be_loosened() -> None:
    """A constants file cannot raise the deletions cap or shorten the pause."""
    loose = {'engines': {'tgedit': {'max_deletes': 99, 'pause_sec': 1}}}
    params = worker.load_edit_params(loose)
    assert params.max_deletes == tgedit.MAX_DELETES
    assert params.pause_sec == tgedit.MIN_PAUSE_SEC
    assert worker.load_edit_params({}).mode == 'off'


def test_no_token_no_board() -> None:
    """Without a token in the env the editor has no board and stays idle."""
    params = worker.EditParams(board_url='https://board')
    assert worker.board_from_env(params, {}) is None
    got = worker.board_from_env(params, {worker.TOKEN_ENV: 'x'})
    assert isinstance(got, boardapi.Board)


# ----------------------------------------------------------- the board


def _answer(text: str, *, error: bool = False) -> dict[str, object]:
    """Return a JSON-RPC answer carrying ``text``."""
    result = {'content': [{'type': 'text', 'text': text}], 'isError': error}
    return {'jsonrpc': '2.0', 'id': 1, 'result': result}


def test_the_board_answer_is_unwrapped_from_its_summary_line() -> None:
    """A summary line, then the JSON object; notes are typed."""
    body = {
        'notes': [{'id': 5, 'role': 'strateg', 'text': 'ok #4', 'tags': []}]
    }
    got = boardapi._payload('board', _answer(f'1 note\n\n{json.dumps(body)}'))
    assert boardapi._note(got['notes'][0]).role == 'strateg'  # type: ignore[index]


def test_a_board_error_raises_with_its_reason() -> None:
    """A tool error or a JSON-RPC error is raised, not swallowed."""
    with pytest.raises(boardapi.BoardError, match='forbidden'):
        boardapi._payload('board_post', _answer('forbidden', error=True))
    with pytest.raises(boardapi.BoardError, match='bad token'):
        boardapi._payload('board', {'error': {'message': 'bad token'}})


@pytest.mark.parametrize(
    'text',
    [
        '1 note\n\n{"board": "admin"}',  # no notes field
        '1 note\n\n{"notes": {"id": 5}}',  # notes is not a list
        'board is down',  # no JSON at all
        '1 note\n\n["a list"]',  # JSON, but not an object
    ],
)
def test_a_broken_board_answer_raises_not_looks_empty(
    text: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken answer must not read as "no packages" (the #507 blind spot)."""

    def fake_post(*_: object, **__: object) -> object:
        return _Response(_answer(text))

    monkeypatch.setattr('requests.post', fake_post)
    with pytest.raises(boardapi.BoardError, match='board board'):
        boardapi.Board('https://board', 'token').notes(q='tg-edit')


@dataclass
class _Response:
    """A fake HTTP response carrying one JSON body."""

    body: object

    def raise_for_status(self) -> None:
        """Pretend the status was 200."""

    def json(self) -> object:
        """Return the body."""
        return self.body


def test_an_outcome_is_counted_by_status() -> None:
    """The report's first line counts outcomes by status."""
    assert tgedit.tally([EMPTY, EMPTY]) == {tgedit.DRY_OK: 2}
