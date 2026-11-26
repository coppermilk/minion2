# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The channel editor: parse, dry-run, limits, never-touch, apply, check.

Pure logic (core/tgedit), the worker over a fake account and a directory
of package files (glue/tgedit), and the dry check over a ``telegram``
snapshot (dev/tgedit_check). The dry run is the heart of it: ``old`` that
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
from typing import cast

import pytest

from minion_core.adapters import userchat
from minions.userbot.core import config
from minions.userbot.core import relink
from minions.userbot.core import tgedit
from minions.userbot.dev import tgedit_check
from minions.userbot.glue import tgedit as worker

CHAT = 't.me/da_eto_tyoma_chat'
WATCH = 'Watch'
GUARD = tgedit.Guard(urls=(CHAT,), link_texts=(WATCH,))
AUTHOR = 'audience-head'
CHANNEL = -100
NOW = 1_800_000_000.0
PACKAGE = '475-cleanup'
PAUSE = 10.0
EMPTY = tgedit.Outcome(tgedit.Item(1, tgedit.EDIT, 'x'), tgedit.DRY_OK)


def _file(items: object, **over: object) -> str:
    """Return a package file's text; ``over`` replaces top-level keys."""
    body = {'id': PACKAGE, 'author': AUTHOR, 'why': 'clean up', 'edits': items}
    return json.dumps({**body, **over})


def _parse(items: object, **over: object) -> tgedit.Package:
    """Parse a package file named PACKAGE."""
    return tgedit.parse(_file(items, **over), PACKAGE)


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


@pytest.mark.parametrize(
    ('text', 'says'),
    [
        ('{"id": ', 'does not parse'),
        ('[]', 'JSON object'),
        (_file([{'post': 1, 'action': 'delete'}], extra=1), 'unknown key'),
        (_file([{'post': 1, 'action': 'delete'}], id='476-x'), 'file name'),
        (_file([], id=PACKAGE), 'non-empty list'),
        (_file([{'post': 1, 'action': 'delete'}], author=' '), 'author'),
        (_file([{'post': 1, 'action': 'delete'}], why='a\nb'), 'one line'),
        (_file([{'post': 1, 'action': 'move'}]), "action 'move'"),
        (_file([{'post': 1, 'action': 'delete', 'x': 1}]), 'edit 1: unknown'),
        (_file([{'post': 5, 'action': 'desc', 'new': 'x'}]), 'needs post 0'),
        (_file([{'post': 0, 'action': 'edit', 'old': 'x'}]), 'needs post >0'),
        (_file([{'post': 1, 'action': 'edit'}]), 'non-empty old/new'),
        (_file([{'post': -1, 'action': 'delete'}]), 'whole number'),
        (_file([{'post': True, 'action': 'delete'}]), 'whole number'),
    ],
)
def test_a_broken_package_is_refused_whole(text: str, says: str) -> None:
    """A typo stops the package before anything is touched."""
    with pytest.raises(tgedit.PackageError, match=None) as caught:
        tgedit.parse(text, PACKAGE)
    assert says in str(caught.value)
    assert str(caught.value).startswith(PACKAGE)


@pytest.mark.parametrize('name', ['cleanup', '475', '475-Cleanup', '475-'])
def test_a_package_id_is_a_number_dash_word(name: str) -> None:
    """The id names the file and is what ``tgedit.json`` remembers."""
    text = _file([{'post': 1, 'action': 'delete'}], id=name)
    with pytest.raises(tgedit.PackageError, match='<number>-<word>'):
        tgedit.parse(text, name)


def test_fifteen_deletions_pass_sixteen_refuse_the_package() -> None:
    """At most 15 deletions a package; the config may not raise that."""
    fifteen = tuple(tgedit.Item(n, tgedit.DELETE) for n in range(1, 16))
    tgedit.check_limits(fifteen, 15)
    sixteen = (*fifteen, tgedit.Item(99, tgedit.DELETE))
    with pytest.raises(tgedit.PackageError, match='16 deletions'):
        tgedit.check_limits(sixteen, 99)


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


def _world(tmp_path: Path, items: object = None) -> _Account:
    """Ship one package file into ``tmp_path/packages``; return the account."""
    packages = tmp_path / 'packages'
    packages.mkdir(exist_ok=True)
    (packages / f'{PACKAGE}.json').write_text(
        _file(ITEMS if items is None else items), 'utf-8'
    )
    return _Account(
        {
            1142: _linked(1142, 'old tour'),
            1167: _linked(1167, 'it is gov and blog.'),
            944: _linked(944, 'support'),
            1000: _linked(1000, 'not in the package'),
        }
    )


def _editor(
    tmp_path: Path, account: _Account, mode: str
) -> tuple[worker.ChannelEditor, list[float]]:
    """Build an editor in ``mode``; return it and the pauses it took."""
    slept: list[float] = []

    async def sleep(sec: float) -> None:
        slept.append(sec)

    params = worker.EditParams(mode=mode, pause_sec=PAUSE, guard=GUARD)
    editor = worker.ChannelEditor(
        worker.EditDeps(
            account=cast('userchat.Account', account),
            channel=lambda: CHANNEL,
            packages=tmp_path / 'packages',
            path=tmp_path / worker.STATE_FILE,
            journal=tmp_path / worker.JOURNAL_FILE,
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
    """Off touches neither the channel nor the journal."""
    account = _world(tmp_path)
    editor, _ = _editor(tmp_path, account, 'off')
    assert asyncio.run(editor.tick(NOW)) == 0
    assert account.writes == []
    assert not (tmp_path / worker.JOURNAL_FILE).exists()


def test_dry_logs_and_journals_every_item_once_and_writes_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Dry: every item's outcome goes to the log and the journal, once."""
    account = _world(tmp_path)
    editor, _ = _editor(tmp_path, account, 'dry')
    with caplog.at_level(logging.INFO, logger='userbot'):
        assert asyncio.run(editor.tick(NOW)) == 1
    assert account.writes == []
    assert f'{PACKAGE}: post 1167 . edit -> dry_ok' in caplog.text
    assert f'{PACKAGE}: post 944 . edit -> skipped (not_found)' in caplog.text
    rows = _journal(tmp_path, 'outcome')
    assert len(rows) == len(ITEMS)
    assert {row['mode'] for row in rows} == {'dry'}
    assert asyncio.run(editor.tick(NOW)) == 0
    assert len(_journal(tmp_path, 'outcome')) == len(ITEMS)


def test_on_applies_journals_paces_and_reports(tmp_path: Path) -> None:
    """On: what passed is written, 10 s apart, after its before-image."""
    account = _world(tmp_path)
    editor, slept = _editor(tmp_path, account, 'on')
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
    assert asyncio.run(editor.tick(NOW)) == 0  # applied once, never twice


def test_a_dry_reported_package_is_still_applied_once_on(
    tmp_path: Path,
) -> None:
    """Dry, then on: the dry report does not count as applied."""
    account = _world(tmp_path)
    dry, _ = _editor(tmp_path, account, 'dry')
    asyncio.run(dry.tick(NOW))
    on, _ = _editor(tmp_path, account, 'on')
    assert asyncio.run(on.tick(NOW)) == 1
    assert account.writes
    assert worker.read_state(tmp_path / worker.STATE_FILE).done == {PACKAGE}


def test_only_json_files_are_packages(tmp_path: Path) -> None:
    """A README or a draft beside the packages is not read."""
    account = _world(tmp_path)
    (tmp_path / 'packages' / 'README.md').write_text('notes', 'utf-8')
    editor, _ = _editor(tmp_path, account, 'dry')
    assert asyncio.run(editor.tick(NOW)) == 1


def test_a_broken_package_is_refused_whole_and_said_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A file that does not parse: nothing written, refusal journaled."""
    account = _world(tmp_path)
    (tmp_path / 'packages' / f'{PACKAGE}.json').write_text('{', 'utf-8')
    editor, _ = _editor(tmp_path, account, 'on')
    asyncio.run(editor.tick(NOW))
    assert account.writes == []
    assert 'does not parse' in caplog.text
    assert _journal(tmp_path, 'refused')


def test_too_many_deletions_refuse_the_package(tmp_path: Path) -> None:
    """Sixteen deletions: refused whole, journaled, nothing deleted."""
    many = [{'post': n, 'action': 'delete'} for n in range(1, 17)]
    account = _world(tmp_path, many)
    editor, _ = _editor(tmp_path, account, 'on')
    asyncio.run(editor.tick(NOW))
    assert account.writes == []
    assert '16 deletions' in str(_journal(tmp_path, 'refused')[0]['why'])


def test_without_rights_every_item_says_so(tmp_path: Path) -> None:
    """An account that may not edit the channel edits nothing."""
    account = _world(tmp_path)
    account.granted = frozenset()
    editor, _ = _editor(tmp_path, account, 'on')
    asyncio.run(editor.tick(NOW))
    assert account.writes == []
    reasons = [row['reason'] for row in _journal(tmp_path, 'outcome')]
    assert reasons == [tgedit.NO_RIGHTS] * len(ITEMS)


def test_an_unreadable_channel_retries_later(tmp_path: Path) -> None:
    """A failed read is no 'post gone': the package stays pending."""
    account = _world(tmp_path)
    account.unreadable = True
    editor, _ = _editor(tmp_path, account, 'on')
    with pytest.raises(worker.ChannelUnreadableError):
        asyncio.run(editor.tick(NOW))
    assert editor.state.done == set()
    account.unreadable = False
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


def test_the_hard_limits_cannot_be_loosened() -> None:
    """A constants file cannot raise the deletions cap or shorten the pause."""
    loose = {'engines': {'tgedit': {'max_deletes': 99, 'pause_sec': 1}}}
    params = worker.load_edit_params(loose)
    assert params.max_deletes == tgedit.MAX_DELETES
    assert params.pause_sec == tgedit.MIN_PAUSE_SEC
    assert worker.load_edit_params({}).mode == 'off'


@pytest.mark.parametrize(
    'path', sorted(worker.PACKAGES_DIR.glob('*.json')), ids=lambda p: p.stem
)
def test_every_shipped_package_parses_within_the_limits(path: Path) -> None:
    """A merged package is approved, so a typo must fail CI, not the NAS."""
    package = tgedit.parse(path.read_text('utf-8'), path.stem)
    tgedit.check_limits(package.items, tgedit.MAX_DELETES)


def test_the_engine_has_no_door_to_the_board() -> None:
    """Approval is the merge: neither half of the engine reads a board."""
    for module in (tgedit, worker):
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


def test_the_dry_check_prints_one_line_per_item(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command prints the tally, then each item in the log's words."""
    package = tmp_path / f'{PACKAGE}.json'
    package.write_text(
        _file([{'post': 1, 'action': 'edit', 'old': 'a', 'new': 'b'}]),
        'utf-8',
    )
    snapshot = tmp_path / 'telegram.json'
    snapshot.write_text(_snapshot(_snap(1, 'a')), 'utf-8')
    assert tgedit_check.main([str(package), str(snapshot)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith(f'{PACKAGE}: 1 items')
    assert len(out) == 2  # noqa: PLR2004
    snapshot.write_text('not json', 'utf-8')
    assert tgedit_check.main([str(package), str(snapshot)]) == 1


def test_an_outcome_is_counted_by_status() -> None:
    """The report's first line counts outcomes by status."""
    assert tgedit.tally([EMPTY, EMPTY]) == {tgedit.DRY_OK: 2}
