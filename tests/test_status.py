# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""A golden render of /status: the report is the operator's whole view.

/status is assembled from six engines by a dozen small section builders, so
a refactor there is easy to make and hard to eyeball. This pins the WHOLE
text for one fixed state: any change to spacing, ordering, glyphs or wording
shows up as a diff, and a deliberate change is a one-line update here.
"""

from __future__ import annotations

import ast
import pathlib
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


from minion_core.pace import Lane
from minions.userbot import main
from minions.userbot.core import config
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
from minions.userbot.glue import status
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
            'watched': '[w]',
            'liked': '[l]',
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
    store = StateStore(tmp_path / 'reactions.db')
    brain = reactions.ReactionBrain(params, store)
    brain.clock = lambda: NOW
    brain.state.mood = 0.25
    brain.state.alive = {'12': 5.0, '13': 3.0}
    brain.state.posts = [(TARGET, 77)]
    store.mark(f'{TARGET}:77:alice')
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
    brain.ledger.add_take('alice', 3, brain._control(), NOW)
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
        stories.StoryParams(enabled=True, poll_sec=1800.0),
        StateStore(tmp_path / 'stories.db'),  # its own file, as in production
    )
    # One glance covering every verdict a peer can get: being opened,
    # passed over this time, and nothing we have not already seen -- plus
    # one from the archived feed, which is watched on the same maths.
    bot.stories.last_glance = stories.Glance(
        at=NOW - 250,
        peers=(
            stories.Seen(
                PEER_A,
                5,
                5,
                3,
                stories.VIEWING,
                standing=stories.Standing(offered=10, viewed=8, reacted=2),
            ),
            stories.Seen(
                PEER_B,
                4,
                4,
                0,
                stories.PASSED,
                standing=stories.Standing(offered=6, viewed=2),
            ),
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
. all time . 1 commenters . [w] 75% [l] 0% . warmth 0.00
    @alice . [w] 75% [l] 0%
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
. glance 4m 10s ago . 4 with stories
. viewing (1):
    @alice . 3 of 5 new . [w] 80% [l] 25% . in ~3m 10s
. passed this glance (2):
    @bob . 4 new . [w] 33% [l] 0%
    @carol (archived) . 2 new . first time
. already seen (1):
    @dave . 3 up . first time

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


def test_a_blocked_session_names_its_reason_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held session says why in the group header, not on every row.

    Repeating "cooldown 4949s" against each person is exactly the
    duplication this layout replaced: the reason is a property of the
    session, not of the people.
    """
    monkeypatch.setattr(time, 'time', lambda: NOW)
    bot = _bot(tmp_path)
    bot.stories.last_glance = stories.Glance(
        at=NOW - 10,
        peers=(
            stories.Seen(PEER_A, 2, 2, 0, 'cooldown 4949s'),
            stories.Seen(PEER_B, 1, 1, 0, 'cooldown 4949s'),
        ),
        blocked='cooldown 4949s',
    )
    bot.story_watch.pending = []

    got = bot.report.text({PEER_A: '@alice', PEER_B: '@bob'})

    assert '. cooldown 4949s (2):' in got
    assert got.count('cooldown 4949s') == 1
    assert '    @alice . 2 new . first time' in got


# --- the fixture above is not the file the bot ships -----------------------


def _glyph_keys_the_report_asks_for() -> set[str]:
    """Every status key ``glue/status.py`` looks up, read off its own AST."""
    source = pathlib.Path(status.__file__).read_text('utf-8')
    wanted = set()
    for node in ast.walk(ast.parse(source)):
        called = isinstance(node, ast.Call) and isinstance(
            node.func, ast.Attribute
        )
        if not called or node.func.attr not in {'_glyph', '_header'}:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant):
            wanted.add(first.value)
    return wanted


def test_the_shipped_constants_carry_every_status_glyph() -> None:
    """The real JSON covers every icon the report asks for.

    GOLDEN above renders from a fixture that lists all the keys, so it passes
    whatever the shipped file contains -- which is how 'services' came to be
    the one section with no icon in production while the test showed [C].
    Two sources, and until now nothing compared them.
    """
    shipped = config.load_constants(config.CONSTANTS_PATH).status
    missing = _glyph_keys_the_report_asks_for() - set(shipped)
    assert not missing, f'texts.status is missing {sorted(missing)}'


# --- the story section names each person once -----------------------------


def _section(text: str, head: str) -> str:
    """Return one /status section, header to the blank line that ends it."""
    return text.split(head, 1)[1].split('\n\n', 1)[0]


def test_the_story_list_names_each_person_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One row per person, and one aggregate line under them.

    The glance already carries each peer's standing and closes on the
    ledger's aggregate. A second readout over the same ledger printed that
    aggregate twice and then listed the same names again -- invisible to a
    fixture whose story ledger is empty, which is why this one fills it.
    """
    monkeypatch.setattr(time, 'time', lambda: NOW)
    bot = _bot(tmp_path)
    control, ledger = bot.stories._control(), bot.stories.ledger
    for peer, taken in ((PEER_A, 4), (PEER_B, 2)):
        ledger.add_take(str(peer), taken, control, NOW - 86400)
        ledger.remember(str(peer), f'@peer{peer}')

    got = _section(bot.report.text({PEER_A: '@alice', PEER_B: '@bob'}), '[S]')

    assert got.count('@alice') == 1
    assert got.count('@bob') == 1
    assert len([ln for ln in got.splitlines() if ' all ' in ln]) == 1


def test_a_person_in_the_list_is_named_not_numbered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The raw id is Routing's business, not the reader's.

    _chat_label appends it because Routing is read to CONFIGURE chats; a
    list of people is read to recognise them.
    """
    monkeypatch.setattr(time, 'time', lambda: NOW)
    bot = _bot(tmp_path)

    got = _section(bot.report.text({PEER_A: f'@alice ({PEER_A})'}), '[S]')

    assert '@alice' in got
    assert str(PEER_A) not in got


def test_a_viewing_row_counts_the_new_stories_not_all_of_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening some of what is new, and skipping the rest, must be visible.

    Counted against the peer's whole active set instead, "3 of 5" hid
    whether the other two were old or deliberately passed over.
    """
    monkeypatch.setattr(time, 'time', lambda: NOW)
    bot = _bot(tmp_path)
    bot.stories.last_glance = stories.Glance(
        at=NOW - 10,
        peers=(stories.Seen(PEER_A, active=9, unseen=4, viewing=3),),
    )

    got = _section(bot.report.text({PEER_A: '@alice'}), '[S]')

    assert '3 of 4 new' in got
    assert '9' not in got.split('viewing (1):')[1].splitlines()[1]


def test_people_with_nothing_new_are_still_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Everyone with stories up is named, even when nothing is pending.

    A story lives a day and we open it once, so for the rest of that day
    every peer with stories up falls in this group -- reducing it to a count
    left the section about people naming nobody at all, which is what a real
    report looked like.
    """
    monkeypatch.setattr(time, 'time', lambda: NOW)
    bot = _bot(tmp_path)
    bot.stories.last_glance = stories.Glance(
        at=NOW - 10,
        peers=(
            stories.Seen(PEER_A, active=3, unseen=0),
            stories.Seen(PEER_B, active=1, unseen=0),
        ),
    )

    got = _section(bot.report.text({PEER_A: '@alice', PEER_B: '@bob'}), '[S]')

    assert '. already seen (2):' in got
    assert '    @alice . 3 up . first time' in got
    assert '    @bob . 1 up . first time' in got
    assert 'viewing' not in got
