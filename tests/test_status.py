# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""A golden render of /status: the report is the operator's whole view.

/status is assembled from six engines by a dozen small section builders, so
a refactor there is easy to make and hard to eyeball. This pins the WHOLE
text for one fixed state: any change to spacing, ordering, glyphs or wording
shows up as a diff, and a deliberate change is a one-line update here.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import TYPE_CHECKING

from tests.conftest import install_telethon_stub

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

install_telethon_stub()

from minions.userbot import main  # noqa: E402
from minions.userbot.core.models import Config  # noqa: E402
from minions.userbot.core.models import Consts  # noqa: E402
from minions.userbot.core.models import Group  # noqa: E402
from minions.userbot.core.models import Posted  # noqa: E402
from minions.userbot.engines import greeter  # noqa: E402
from minions.userbot.engines import reactions  # noqa: E402
from minions.userbot.engines import stories  # noqa: E402
from minions.userbot.glue import stories as stories_glue  # noqa: E402
from minions.userbot.glue import users as users_glue  # noqa: E402

NOW = 1_760_000_000.0  # a fixed clock, so every eta in the text is stable
SOURCE = -1001
TARGET = -1002


async def _unused_label(peer_id: int) -> str:
    """Peer resolver the disabled story section never reaches."""
    return str(peer_id)


def _consts() -> Consts:
    """Ascii stand-ins for the JSON glyphs, so the golden text stays ASCII."""
    return Consts(
        fields={},
        action_value='',
        author='',
        announce=[''],
        love=[''],
        lead=[''],
        arrow_down=[''],
        view_label=['View'],
        column_separator='|',
        rows=[],
        platform_emoji={},
        sample_short='',
        sample_long='',
        status_help='legend text',
        help_text='',
        help_hint='',
        human_words=(),
        status={
            'title': '[T]',
            'routing': '[R]',
            'videos': '[V]',
            'reactions': '[K]',
            'greeter': '[G]',
            'users': '[U]',
            'stories': '[S]',
            'services': '[C]',
            'legend': '[L]',
            'on': '(+)',
            'off': '(-)',
            'bullet': '.',
            'arrow': '->',
        },
        emoji_all=[],
    )


def _bot(tmp_path: Path) -> main.Userbot:
    """Build a Userbot on real engines over tmp state and a frozen clock."""
    bot = object.__new__(main.Userbot)
    bot.consts = _consts()
    bot.config = Config(
        source=SOURCE,
        targets=(TARGET,),
        test_target=0,
        platforms=('tiktok', 'youtube'),
        threshold=0.9,
        timeout=10800.0,
        backfill=100,
        max_duration=180,
        repost_guard=604800.0,
        repost_guard_count=5,
        discussion_gap=2.0,
    )
    bot.mode = 'live'
    bot._modes = {
        'aggregator': 'live',
        'reactions': 'live',
        'stories': 'off',
        'users': 'off',
        'greeter': 'live',
    }
    bot.groups = [Group(title='Waiting one', created_at=NOW - 600)]
    bot.posted = [
        Posted(
            title='Posted one',
            at='2026-08-01T10:00:00Z',
            links={'a': 'u'},
            msg_ids=[1],
        )
    ]
    bot.rejected = {'long video'}

    params = reactions.ReactionParams(
        enabled=True,
        attach_enabled=True,
        active_start=7.0,
        active_end=17.0,
        tz_offset_hours=0.0,
        pool=(reactions.ReactionEmoji('11', 'a', 1.0, ()),),
        like_pool=(reactions.ReactionEmoji('22', 'b', 1.0, ()),),
    )
    brain = reactions.ReactionBrain(params, tmp_path / 'r.json')
    brain.clock = lambda: NOW
    brain.state.mood = 0.25
    brain.state.alive = {'12': 5.0, '13': 3.0}
    brain.state.posts = [(TARGET, 77)]
    brain.state.reacted = {f'{TARGET}:77:alice'}
    brain.state.pending = [
        {
            'chat': TARGET,
            'reply_to': 90,
            'root': 77,
            'when': NOW + 300,
            'text': 'nice one',
            'emojis': [['11', 'a']],
            'kind': 'react',
        },
    ]
    brain.state.ledger.add_take('alice', 3)
    brain.state.ledger.add_offer('alice', 1)
    brain.state.ledger.remember('alice', '@alice')
    bot.reactions = brain
    bot._rescan_sec = 300.0
    bot._react_next_rescan = NOW + 120

    bot.stories = stories.StoryBrain(
        stories.StoryParams(enabled=False), tmp_path / 's.json'
    )
    bot.story_watch = stories_glue.StoryWatch(
        stories_glue.StoryDeps(
            client=SimpleNamespace(),
            brain=bot.stories,
            source=SOURCE,
            label=_unused_label,
        )
    )

    gparams = greeter.GreeterParams(
        enabled=True,
        channel=TARGET,
        poll_sec=600.0,
        max_dm_per_day=5,
        tz_offset_hours=0.0,
        wake_start_hour=7.0,
        wake_end_hour=17.0,
    )
    grt = greeter.Greeter(
        SimpleNamespace(), gparams, greeter.GreeterIO(tmp_path / 'g.json')
    )
    grt.state.dm_today = 2
    grt.state.last_event_id = 41
    grt.next_sync = NOW + 60
    grt.deferred = 0
    bot.greeter = grt

    bot.audience = users_glue.AudienceLog(
        users_glue.AudienceDeps(
            client=SimpleNamespace(),
            source=SOURCE,
            store=SimpleNamespace(summary=dict),
            watched=set,
        )
    )
    return bot


GOLDEN = """\
[T] Userbot . (+) LIVE

[R] Routing
. source: @src (-1001)
. target: @dst (-1002)
. posting -> @dst (-1002)

[V] Videos . pending 1 (timeout 3h 0m) . posted 1 . rejected 1 . guard \
168h 0m/last 5
. "Waiting one" have [-] wait [tiktok, youtube] -> ~2h 50m
. "Posted one" . 2026-08-01 . 1 links

[K] Reactions . (+) on . 1 reactions / 1 likes
. likes -> <tg-emoji emoji-id="22">b</tg-emoji>
. reactions -> <tg-emoji emoji-id="11">a</tg-emoji>
. mood 0.25 . answered 1 . pending 1
. window 7-17h (prior) . learned 12h, 13h
. rescan 300s -> next 08:55 (in 2m 0s)
. today likes 0/400 . stickers 0/40
. attach 1 commenters -> p~0.75 r~0.00
    @alice  A 0.00 . p 0.75 r 0.00
. watching 1 posts:
    @dst (-1002): 77
. queued:
    <tg-emoji emoji-id="11">a</tg-emoji> like -> "nice one" . post 77 . \
in ~5m 0s
. /reactnow . /requeue

[G] Greeter . (+) on . DMs 2/5 . last event 41
. check 600s -> next 08:54 (in 1m 0s)

[U] Users DB . (-) off
[S] Stories . (-) off

[C] Services
. (+) aggregator: LIVE
   /aggregator_on  /aggregator_off  /aggregator_test  /aggregator_live
. (+) reactions: LIVE
   /reactions_on  /reactions_off  /reactions_test  /reactions_live
. (-) stories: OFF
   /stories_on  /stories_off  /stories_test  /stories_live
. (-) users: OFF
   /users_on  /users_off  /users_test  /users_live
. (+) greeter: LIVE
   /greeter_on  /greeter_off  /greeter_test  /greeter_live

[L] legend text"""


def test_status_text_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole /status render, pinned. A diff here is a deliberate change."""
    monkeypatch.setattr(time, 'time', lambda: NOW)
    bot = _bot(tmp_path)
    labels = {SOURCE: '@src (-1001)', TARGET: '@dst (-1002)'}
    assert bot._status_text(labels) == GOLDEN
