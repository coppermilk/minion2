# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Channel comment counts for the site's inbox (task 213, part B)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

from minion_core.adapters import userchat
from minions.userbot.core import comments
from minions.userbot.glue.comments import POSTS
from minions.userbot.glue.comments import CommentCounter
from minions.userbot.glue.comments import CounterDeps

if TYPE_CHECKING:
    from pathlib import Path

NOW = 1_764_230_400.0  # 2025-11-27T08:00:00Z
CHANNEL = -100123
FOUR = 4


def test_a_channel_post_reads_its_comment_count() -> None:
    """``replies.comments`` marks the discussion count of a channel post."""
    assert (
        userchat._comments(SimpleNamespace(comments=True, replies=FOUR))
        == FOUR
    )
    unset = SimpleNamespace(comments=True, replies=None)
    assert userchat._comments(unset) == 0


def test_a_post_without_a_discussion_has_no_count() -> None:
    """No replies, or a group's reply thread: None, never a fake zero."""
    assert userchat._comments(None) is None
    thread = SimpleNamespace(comments=False, replies=7)
    assert userchat._comments(thread) is None


def test_counts_keep_posts_that_take_comments_newest_first() -> None:
    """Zero is a count; a post with comments off is left out."""
    posts = [
        userchat.Msg(id=1170, comments=3),
        userchat.Msg(id=1175, comments=0),
        userchat.Msg(id=1174, comments=None),
    ]
    assert list(comments.counts(posts).items()) == [('1175', 0), ('1170', 3)]


def test_body_says_when_the_channel_takes_no_comments() -> None:
    """No post takes comments: discussion False, so the site can say why."""
    got = comments.body({}, NOW)
    assert got == {
        'v': 1,
        'generated_at': '2025-11-27T08:00:00Z',
        'discussion': False,
        'posts': {},
    }
    assert comments.body({'1': 2}, NOW)['discussion'] is True


def _counter(
    tmp_path: Path, rows: list[userchat.Msg]
) -> tuple[CommentCounter, list[userchat.Slice]]:
    asked: list[userchat.Slice] = []

    async def history(chat: int, piece: userchat.Slice) -> list[userchat.Msg]:
        assert chat == CHANNEL
        asked.append(piece)
        return rows

    deps = CounterDeps(
        account=SimpleNamespace(history=history),  # type: ignore[arg-type]
        channel=lambda: CHANNEL,
        outbox=tmp_path / 'outbox',
    )
    return CommentCounter(deps), asked


def test_count_writes_the_outbox_file_from_one_read(tmp_path: Path) -> None:
    """One history read of the last POSTS posts becomes tg-comments.json."""
    counter, asked = _counter(tmp_path, [userchat.Msg(id=1175, comments=2)])
    assert asyncio.run(counter.count(NOW)) is True
    assert asked == [userchat.Slice(limit=POSTS)]
    path = tmp_path / 'outbox' / comments.OUTBOX_FILE
    assert json.loads(path.read_text())['posts'] == {'1175': 2}


def test_an_empty_read_keeps_the_last_file(tmp_path: Path) -> None:
    """A failed read must not tell the site the channel lost its comments."""
    good, _ = _counter(tmp_path, [userchat.Msg(id=1175, comments=2)])
    asyncio.run(good.count(NOW))
    empty, _ = _counter(tmp_path, [])
    assert asyncio.run(empty.count(NOW + 60)) is False
    path = tmp_path / 'outbox' / comments.OUTBOX_FILE
    assert json.loads(path.read_text())['posts'] == {'1175': 2}
