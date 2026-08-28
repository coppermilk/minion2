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

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


from minion_core.pace import Lane
from minions.userbot import main
from minions.userbot.core.humanize import Variety
from minions.userbot.core.models import Config
from minions.userbot.core.models import Consts
from minions.userbot.core.models import Emoji
from minions.userbot.core.models import Group
from minions.userbot.core.models import Posted
from minions.userbot.core.render import Glyphs
from minions.userbot.core.state import StateStore
from minions.userbot.engines import greeter
from minions.userbot.engines import reactions
from minions.userbot.engines import stories
from minions.userbot.glue import aggregator as aggregator_glue
from minions.userbot.glue import reactions as reactions_glue
from minions.userbot.glue import stories as stories_glue
from minions.userbot.glue import users as users_glue
from minions.userbot.glue.status import StatusReport

NOW = 1_760_000_000.0  # a fixed clock, so every eta in the text is stable
SOURCE = -1001
TARGET = -1002
PEER_A = 5001
PEER_B = 5002
PEER_C = 5003
PEER_D = 5004


async def _unused_posted(target: int, post_id: int) -> None:
    """Post hand-off the render never reaches."""


async def _unused_announce(text: str) -> None:
    """Operator channel the render never uses."""


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
            'schedule': '[H]',
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
    )
    bot.mode = 'live'
    service_modes = {
        'aggregator': 'live',
        'reactions': 'live',
        'stories': 'live',
        'users': 'off',
        'greeter': 'live',
    }
    bot.modes = SimpleNamespace(mode_of=service_modes.__getitem__)
    bot.aggregator = aggregator_glue.LinkAggregator(
        aggregator_glue.AggregatorDeps(
            account=None,
            config=bot.config,
            consts=bot.consts,
            state_path=tmp_path / 'agg.json',
            targets=lambda: (TARGET,),
            on_posted=_unused_posted,
            field_keys=(),
            variety=Variety(),
        ),
        groups=[Group(title='Waiting one', created_at=NOW - 600)],
        posted=[
            Posted(
                title='Posted one',
                at='2026-08-01T10:00:00Z',
                links={'a': 'u'},
                msg_ids=[1],
            )
        ],
        rejected={'long video'},
    )

    params = reactions.ReactionParams(
        enabled=True,
        attach_enabled=True,
        active_start=7.0,
        active_end=17.0,
        tz_offset_hours=0.0,
        pool=(Emoji('11', 'a', base=1.0, tags=()),),
        like_pool=(Emoji('22', 'b', base=1.0, tags=()),),
    )
    store = StateStore(tmp_path / 'peers.db', tmp_path / 'cursors.json')
    brain = reactions.ReactionBrain(params, store)
    brain.clock = lambda: NOW
    brain.state.mood = 0.25
    brain.state.alive = {'12': 5.0, '13': 3.0}
    brain.state.posts = [(TARGET, 77)]
    store.mark(reactions.ENGINE, f'{TARGET}:77:alice')
    brain.state.pending = [
        reactions.Reaction(
            chat=TARGET,
            reply_to=90,
            root=77,
            when=NOW + 300,
            text='nice one',
            emojis=(('11', 'a'),),
        ),
    ]
    brain.ledger.add_take('alice', 3)
    brain.ledger.add_offer('alice', 1)
    brain.ledger.remember('alice', '@alice')
    bot.reactions = brain
    bot.comment_watch = reactions_glue.CommentWatch(
        reactions_glue.CommentDeps(
            account=None,
            brain=brain,
            targets=lambda: (TARGET,),
            announce=_unused_announce,
            glyphs=Glyphs('.', '->'),
            rescan_sec=300.0,
        ),
        next_rescan=NOW + 120,
    )

    bot.stories = stories.StoryBrain(
        stories.StoryParams(enabled=True, poll_sec=1800.0), store
    )
    # One glance covering every verdict a peer can get: being opened,
    # passed over this time, and nothing we have not already seen -- plus
    # one from the archived feed, which is watched on the same maths.
    bot.stories.last_glance = stories.Glance(
        at=NOW - 250,
        peers=(
            stories.Seen(PEER_A, 5, 5, 3, stories.VIEWING),
            stories.Seen(PEER_B, 4, 4, 0, stories.PASSED),
            stories.Seen(PEER_C, 2, 2, 0, stories.PASSED, hidden=True),
            stories.Seen(PEER_D, 3, 0, 0, stories.NOTHING_NEW),
        ),
    )
    bot.story_watch = stories_glue.StoryWatch(
        stories_glue.StoryDeps(
            account=None,
            brain=bot.stories,
            source=SOURCE,
            label=_unused_label,
        )
    )
    bot.story_watch.next_poll = NOW + 900
    bot.story_watch.pending = [
        stories.StoryView(PEER_A, (51, 52, 53), 53, NOW + 190)
    ]

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
            account=None,
            source=SOURCE,
            store=SimpleNamespace(summary=dict),
            watched=set,
        )
    )
    # The host's own loop and probe, and the gate behind the door: fixed
    # so every countdown in the golden text is stable. One lane is widened
    # so the flood line is covered too.
    bot.next_tick = NOW + 42
    bot._last_probe = NOW - 100
    bot._probe_interval = 300.0
    bot.account = SimpleNamespace(
        pacing=lambda: [
            Lane('dm', 44.0, 2.0),
            Lane('read', 0.0, 1.0),
            Lane('write', 3.0, 1.0),
            # Under a second: reads as free, not as 'in 0s'.
            Lane('story', 0.4, 1.0),
        ]
    )
    bot.report = StatusReport(bot)
    return bot


GOLDEN = """\
[T] Userbot . (+) LIVE

[R] Routing
. source: @src (-1001)
. target: @dst (-1002)
. posting -> @dst (-1002)

[V] Videos . pending 1 (timeout 3h 0m) . posted 1 . rejected 1 . guard 168h \
0m/last 5
. "Waiting one" have [-] wait [tiktok, youtube] -> ~2h 50m
. "Posted one" . 2026-08-01 . 1 links

[K] Reactions . (+) on . 1 reactions / 1 likes
. likes -> <tg-emoji emoji-id="22">b</tg-emoji>
. reactions -> <tg-emoji emoji-id="11">a</tg-emoji>
. mood 0.25 . answered 1 . pending 1
. window 7-17h (prior) . learned 12h, 13h
. today likes 0/400 . stickers 0/40
. attach 1 commenters -> p~0.75 r~0.00
    @alice  A 0.00 . p 0.75 r 0.00
. watching 1 posts:
    @dst (-1002): 77
. queued:
    <tg-emoji emoji-id="11">a</tg-emoji> like -> "nice one" . post 77 . in \
~5m 0s
. /reactnow . /requeue

[G] Greeter . (+) on . DMs 2/5 . last event 41

[U] Users DB . (-) off
[S] Stories . (+) on . 0 today . 0/50 reacted . 1 queued . next view -> in \
3m 10s
. glance 4m 10s ago . 4 with stories (1 archived) . viewing 1 . passed 2 . \
nothing new 1
. with stories now:
    @alice . 5 up . 5 new . viewing 3 in ~3m 10s
    @bob . 4 up . 4 new . passed this glance
    @carol . 2 up . 2 new . passed this glance . archived feed
    @dave . 3 up . - . nothing new

[H] Schedule
. tick -> in 42s . probe -> in 3m 20s . lookups 0 queued
. reactions rescan -> in 2m 0s . stories poll -> in 15m 0s . greeter check \
-> in 1m 0s
. pace . dm in 44s . read now . write in 3s . story now
. widened by a flood . dm x2.0

[C] Services
. (+) aggregator: LIVE
   /aggregator_on  /aggregator_off  /aggregator_test  /aggregator_live
. (+) reactions: LIVE
   /reactions_on  /reactions_off  /reactions_test  /reactions_live
. (+) stories: LIVE
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
    labels = {
        SOURCE: '@src (-1001)',
        TARGET: '@dst (-1002)',
        PEER_A: '@alice',
        PEER_B: '@bob',
        PEER_C: '@carol',
        PEER_D: '@dave',
    }
    assert bot.report.text(labels) == GOLDEN


def test_rendering_the_report_makes_no_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report reads state; it must not go and ask Telegram anything.

    /status is the command an operator hits most, and the story glance is
    a snapshot ON PURPOSE: re-reading the feed to look current would put
    two story requests behind every report, on an account this whole
    boundary exists to keep quiet.
    """
    monkeypatch.setattr(time, 'time', lambda: NOW)
    bot = _bot(tmp_path)
    asked: list[str] = []
    bot.account = SimpleNamespace(
        pacing=lambda: [Lane('read', 0.0, 1.0)],
        peer=lambda cid: asked.append(str(cid)),
        stories_feed=lambda **_: asked.append('feed'),
    )

    bot.report.text({SOURCE: '@src', TARGET: '@dst'})

    assert asked == []
