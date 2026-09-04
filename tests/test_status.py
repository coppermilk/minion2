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
import re
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


from minion_core.pace import Lane
from minions.userbot import main
from minions.userbot.core import attachment
from minions.userbot.core import config
from minions.userbot.core import relationship
from minions.userbot.core.humanize import Variety
from minions.userbot.core.models import Config
from minions.userbot.core.models import Consts
from minions.userbot.core.models import Emoji
from minions.userbot.core.models import Group
from minions.userbot.core.models import Posted
from minions.userbot.core.state import ACTS
from minions.userbot.core.state import DB_NAME
from minions.userbot.core.state import Actor
from minions.userbot.core.state import Database
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
# 2026-08-01T10:00:00Z as an epoch: the file keeps one time
# format, and the ISO the golden text shows is made in the render.
POSTED_AT = 1785578400.0


async def _unused_posted(target: int, post_id: int) -> None:
    """Post hand-off the render never reaches."""


async def _unused_announce(text: str) -> None:
    """Operator channel the render never uses."""


async def _unused_label(peer_id: int) -> str:
    """Peer resolver the render never reaches (it never asks Telegram)."""
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
            'plan': '[P]',
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


def _known(names: dict[int, str]) -> dict[int, Actor]:
    """Return what the report is handed: fields, not display strings."""
    return {
        peer_id: Actor(peer_id, 'user', username=handle)
        for peer_id, handle in names.items()
    }


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
    # Built here, before the services, exactly as main.__init__ does -- they
    # are handed its words when they are wired.
    bot.report = StatusReport(bot)
    # One database, one view per service -- exactly as main builds it.
    db = Database(tmp_path / DB_NAME)
    bot.aggregator = aggregator_glue.LinkAggregator(
        aggregator_glue.AggregatorDeps(
            account=None,
            config=bot.config,
            consts=bot.consts,
            store=db.store('aggregator'),
            targets=lambda: (TARGET,),
            on_posted=_unused_posted,
            field_keys=(),
            variety=Variety(),
        ),
        groups=[Group(title='Waiting one', created_at=NOW - 600)],
        posted=[
            Posted(
                title='Posted one',
                at=POSTED_AT,
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
    store = db.store('reactions')
    brain = reactions.ReactionBrain(params, store)
    brain.clock = lambda: NOW
    brain.state.mood = 0.25
    # The learned uptime curve is rows now, so it is seeded the way the
    # heartbeat actually builds it: one observation per beat.
    for hour, beats in ((12, 5), (13, 3)):
        for _ in range(beats):
            store.note_hour(hour, params.uptime_half_life_sec, NOW)
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
    brain.ledger.add_take(PEER_A, (1, 2, 3), brain._control(), NOW)
    brain.ledger.add_offer(PEER_A, (4,))
    bot.reactions = brain
    bot.comment_watch = reactions_glue.CommentWatch(
        reactions_glue.CommentDeps(
            account=None,
            brain=brain,
            targets=lambda: (TARGET,),
            announce=_unused_announce,
            # The report's own words, as main.py wires them: a service that
            # renders a row of /status must not invent a second vocabulary.
            glyphs=bot.report.glyphs(),
            rescan_sec=300.0,
        ),
        next_rescan=NOW + 120,
    )

    bot.stories = stories.StoryBrain(
        stories.StoryParams(enabled=True, poll_sec=1800.0),
        db.store('stories'),  # its own view, as in production
    )
    # A record for two of them, written through the ledger rather than made
    # up beside it: in production stories._standing reads THIS store and so
    # does the word, and a fixture that fabricates one of the two lets a
    # golden line claim "like" next to a 0% like column and call it expected.
    led, ctrl = bot.stories.ledger, bot.stories._control()
    then = NOW - 3 * 86400.0  # last week's, so "today" stays a clean zero
    led.add_take(PEER_A, (1, 2, 3, 4, 5, 6, 7, 8), ctrl, then)
    led.add_offer(PEER_A, (9, 10), then)  # 8 of 10 watched
    led.bump_recip(PEER_A, 1, then)
    led.bump_recip(PEER_A, 2, then)  # 2 of those 8 answered
    led.add_take(PEER_B, (1, 2), ctrl, then)
    led.add_offer(PEER_B, (3, 4, 5, 6), then)  # 2 of 6 watched, none answered
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
                standing=bot.stories._standing(PEER_A),
            ),
            stories.Seen(
                PEER_B,
                4,
                4,
                0,
                stories.PASSED,
                standing=bot.stories._standing(PEER_B),
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
            learn=_unused_label,
            name=lambda peer_id: '',
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
        SimpleNamespace(), gparams, greeter.GreeterIO(db.store('greeter'))
    )
    for _ in range(2):
        grt.store.spend('dm', grt._today(), 0)
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
    return bot


GOLDEN = """\
[T] Userbot . (+) LIVE

[R] Routing
. source: @src (-1001)
. target: @dst (-1002)
. posting -> @dst (-1002)

[V] Videos . pending 1 (timeout 3h) . posted 1 . rejected 1 . guard 7d/last 5
. "Waiting one" have [-] wait [tiktok, youtube] -> ~2h
. "Posted one" . 2026-08-01 . 1 links

[K] Reactions . (+) on . 1 reactions / 1 likes
. likes -> <tg-emoji emoji-id="22">b</tg-emoji>
. reactions -> <tg-emoji emoji-id="11">a</tg-emoji>
. mood 0.25 . answered 1 . pending 1
. window 7-17h (prior) . learned 12h, 13h
. today likes 0/400 . stickers 0/40
. all time . 1 commenters . 75/0 . warmth 0.00
    @alice . 75/0
. watching 1 posts:
    @dst (-1002): 77
. /reactnow . /requeue

[P] Plan . 2 queued
    stories . 3 stories @alice . in ~3m
    <tg-emoji emoji-id="11">a</tg-emoji> like -> "nice one" . post 77 . in ~5m

[G] Greeter . (+) on . DMs 2/5 . last event 41

[U] Users DB . (-) off
[S] Stories . (+) on . 0 today . 0/50 reacted . 1 queued . next view -> in 3m
. all time . 2 people . 57/12 . warmth 0.12
. glance 4m ago . 4 with stories
. viewing (1):
    @alice . 3 of 5 new . like . 80/25
. passed this glance (2):
    @bob . 4 new . seen . 33/0
    @carol (archived) . 2 new . new . first time
. already seen (1):
    @dave . 3 up . new . first time

[H] Schedule
. tick -> in 42s . probe -> in 3m . lookups 0 queued
. reactions rescan -> in 2m . stories poll -> in 15m . greeter check -> in 1m
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
    known = _known(
        {
            SOURCE: 'src',
            TARGET: 'dst',
            PEER_A: 'alice',
            PEER_B: 'bob',
            PEER_C: 'carol',
            PEER_D: 'dave',
        }
    )
    assert bot.report.text(known) == GOLDEN


def test_no_span_in_the_report_carries_a_second_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of one format, asserted rather than eyeballed.

    Every duration in every section goes through one helper, so a second
    unit anywhere means a call site slipped past it. Checked by pattern
    because the alternative is reading the report and hoping -- which is
    how the report came to write the same span three ways.
    """
    monkeypatch.setattr(time, 'time', lambda: NOW)
    bot = _bot(tmp_path)
    known = _known({PEER_A: 'alice', PEER_B: 'bob'})

    both = re.findall(r'\d+[smhd]\s+\d+[smhd]', bot.report.text(known))

    assert not both, f'two-unit spans left: {both}'


def test_every_span_in_the_report_uses_the_operator_letters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And they come from the JSON, so a bare ASCII unit is a missed site.

    Marking every unit and then looking for an unmarked one: a call site
    that formats its own duration keeps the English letter and shows up
    here, which is the only way to catch one that never runs in a test.

    The learned-hours line is exempt and stays ASCII on purpose -- '7-17h'
    is an hour of the DAY, not a length of time, and writing it in the
    duration vocabulary would be the drift this test exists to stop.
    """
    monkeypatch.setattr(time, 'time', lambda: NOW)
    bot = _bot(tmp_path)
    bot.consts = replace(
        bot.consts,
        status=bot.consts.status | {f'unit_{u}': f'<{u}>' for u in 'smhd'},
    )
    # The words are handed to a service when it is wired, so re-wiring is
    # what picks up a changed file -- which is what a restart does.
    bot.comment_watch.deps = replace(
        bot.comment_watch.deps, glyphs=bot.report.glyphs()
    )

    got = bot.report.text(_known({PEER_A: 'alice'}))
    spans = [ln for ln in got.splitlines() if not ln.startswith('. window ')]

    assert '<m>' in got  # the lookup really is reached
    left = re.findall(r'\d+[smhd]\b', '\n'.join(spans))

    assert not left, f'spans that skipped the lookup: {left}'


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

    bot.report.text(_known({SOURCE: 'src', TARGET: 'dst'}))

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
            stories.Seen(
                PEER_A,
                2,
                2,
                0,
                'cooldown 4949s',
                standing=bot.stories._standing(PEER_A),
            ),
            stories.Seen(
                PEER_B,
                1,
                1,
                0,
                'cooldown 4949s',
                standing=bot.stories._standing(PEER_B),
            ),
        ),
        blocked='cooldown 4949s',
    )
    bot.story_watch.pending = []

    got = bot.report.text(_known({PEER_A: 'alice', PEER_B: 'bob'}))

    assert '. cooldown 4949s (2):' in got
    assert got.count('cooldown 4949s') == 1
    assert '    @alice . 2 new . like . 80/25' in got


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


def test_the_shipped_constants_carry_a_word_for_every_doing() -> None:
    """And a word for every rung of every ladder, plus the two below them.

    The sweep above cannot see these: the key is built as
    ``f'act_{service}_{act}'``, so a missing one falls back to the bare act
    and drops an English "sticker" into a Russian roster with nothing
    failing. Enumerated from ``ACTS`` rather than listed, so a new service
    or a new rung fails here instead of shipping wordless.

    The service is IN the key because ``like`` is the top of what we ever do
    to a story and the middle of what we do to a comment: one shared key
    would have printed "love bombing" over a bare comment like.

    The duration letters and the service tags are here for the same reason
    and by the same rule: every one of them is looked up by a key the code
    builds, so none of them is visible to the AST sweep above.
    """
    shipped = config.load_constants(config.CONSTANTS_PATH).status
    wanted = (
        {'act_new', 'act_missed'}
        | {f'unit_{unit}' for unit in status._UNIT_KEYS}
        | {f'tag_{service}' for service in ACTS}
        | {
            f'act_{service}_{act}'
            for service, ladder in ACTS.items()
            for act in ladder
        }
    )

    missing = wanted - set(shipped)

    assert not missing, f'texts.status is missing {sorted(missing)}'


def test_the_word_is_looked_up_under_its_own_service(tmp_path: Path) -> None:
    """Rendered against the shipped file, each ladder reads its own entry.

    The coverage test above proves the keys EXIST; this proves they are the
    keys actually used. Both matter, and neither implies the other: drop the
    service from the lookup and every word falls back to the bare English
    act, which no test comparing rung names would notice -- and the bottom
    rung is spelled the same in both ladders, so it is the one that pins the
    lookup down.
    """
    shipped = config.load_constants(config.CONSTANTS_PATH).status
    bot = _bot(tmp_path)
    bot.consts = replace(bot.consts, status=shipped)

    for service, ladder in ACTS.items():
        for rung, act in enumerate(ladder):
            got = bot.report._act_word(service, rung)

            assert got == shipped[f'act_{service}_{act}']
            assert got != act  # never the bare fallback


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
        ledger.add_take(peer, tuple(range(taken)), control, NOW - 86400)

    got = _section(
        bot.report.text(_known({PEER_A: 'alice', PEER_B: 'bob'})), '[S]'
    )

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

    got = _section(bot.report.text(_known({PEER_A: 'alice'})), '[S]')

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

    got = _section(bot.report.text(_known({PEER_A: 'alice'})), '[S]')

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
            stories.Seen(
                PEER_A,
                active=3,
                unseen=0,
                standing=bot.stories._standing(PEER_A),
            ),
            stories.Seen(
                PEER_B,
                active=1,
                unseen=0,
                standing=bot.stories._standing(PEER_B),
            ),
        ),
    )

    got = _section(
        bot.report.text(_known({PEER_A: 'alice', PEER_B: 'bob'})), '[S]'
    )

    assert '. already seen (2):' in got
    assert '    @alice . 3 up . like . 80/25' in got
    assert '    @bob . 1 up . seen . 33/0' in got
    assert 'viewing' not in got


# ------------------------------------------ /who: one person's whole history

WHO_SEEN = 2
WHO_STORY = 401


def _who_bot(tmp_path: Path) -> main.Userbot:
    """Return a bot whose story ledger holds one person's week."""
    bot = _bot(tmp_path)
    bot.modes = SimpleNamespace(
        mode_of=bot.modes.mode_of, service_dir=lambda _name: tmp_path
    )
    bot._dbs = {tmp_path: Database(tmp_path / DB_NAME)}
    bot.database('stories').note_actor(Actor(PEER_A, 'user', username='alice'))
    # /who asserts an exact history, act for act, so it starts from an empty
    # story ledger rather than on top of the one the golden report seeds.
    conn = bot.stories.store.conn
    conn.execute("DELETE FROM contact WHERE service = 'stories'")
    conn.execute("DELETE FROM standing WHERE service = 'stories'")
    conn.commit()
    ledger = bot.stories.ledger
    control = bot.stories._control()
    ledger.add_take(PEER_A, (400, WHO_STORY), control, NOW - 86400)
    ledger.add_recip(PEER_A, (WHO_STORY,), NOW - 86000, 0.0)
    ledger.add_offer(PEER_A, (402,), NOW - 3600)
    return bot


def test_who_lists_every_act_with_one_person(tmp_path: Path) -> None:
    """The question the contact log exists for, answered in one command.

    Not a summary: the acts themselves, newest first, each naming the story
    it was about, in the words the person on the other side would use --
    they saw a view and a heart, not a "take" and a "recip". /status shows
    what these add up to.
    """
    got = _who_bot(tmp_path).report.who(['/who', '@alice'])

    assert got.splitlines()[0] == f'@alice ({PEER_A})'
    assert f'{WHO_SEEN} seen' in got  # the totals line, in story words
    assert got.count('seen #') == WHO_SEEN  # and one line per story opened
    assert f'like #{WHO_STORY}' in got
    assert 'ignore #402' in got  # the chance we let pass is IN the history


def test_who_takes_a_bare_id_as_well_as_a_name(tmp_path: Path) -> None:
    """An operator with an id and no name still gets the history."""
    bot = _who_bot(tmp_path)
    assert bot.report.who(['/who', str(PEER_A)]) == bot.report.who(
        ['/who', '@alice']
    )


def test_who_says_when_the_counters_left_their_own_log_behind(
    tmp_path: Path,
) -> None:
    """A counter that drifted from its history is the thing to shout about.

    Every percentage in /status is computed from the counter, so a counter
    that no longer matches the acts behind it makes the whole readout a
    guess -- silently, until something says otherwise.
    """
    bot = _who_bot(tmp_path)
    quiet = bot.report.who(['/who', '@alice'])
    db = bot.database('stories')
    db.conn.execute(
        'UPDATE standing SET taken = 99 WHERE peer_id = ?', (PEER_A,)
    )
    db.conn.commit()

    assert 'DISAGREES' not in quiet
    assert 'DISAGREES' in bot.report.who(['/who', '@alice'])


def test_who_without_a_name_explains_itself(tmp_path: Path) -> None:
    """No argument is a usage line, not an error and not everybody."""
    bot = _who_bot(tmp_path)
    assert bot.report.who(['/who']).startswith('/who ')
    assert 'never seen' in bot.report.who(['/who', '@nobody'])


def test_who_says_which_leg_of_their_own_arc_a_person_is_on(
    tmp_path: Path,
) -> None:
    """The line that makes the numbers under it make sense.

    Somebody eleven days into a cold shoulder is SUPPOSED to read as
    neglected. Without this line the percentages below are a mystery to be
    re-derived from dates, which is the class of question /who exists to
    stop.
    """
    bot = _who_bot(tmp_path)
    bot.stories.clock = lambda: NOW  # a day after the acts _who_bot writes
    bot.stories.params = replace(
        bot.stories.params,
        arc=relationship.Arc(
            legs=(
                relationship.Leg('honeymoon', 14, exposure=1.0, recip=1.0),
                relationship.Leg('cold', 10, exposure=0.1, recip=0.0),
                relationship.Leg('swing', 21, exposure=0.0, recip=0.0),
            ),
            enabled=True,
        ),
    )

    got = bot.report.who(['/who', '@alice']).splitlines()[1]

    assert 'honeymoon' in got
    assert 'round 1' in got
    assert 'aiming' in got


def test_who_says_nothing_about_an_arc_that_is_not_running(
    tmp_path: Path,
) -> None:
    """No arc configured, no line -- not an empty one and not "none"."""
    got = _who_bot(tmp_path).report.who(['/who', '@alice']).splitlines()
    assert 'round' not in got[1]


_ARC = relationship.Arc(
    legs=(
        relationship.Leg('honeymoon', 14, exposure=1.0, recip=1.0),
        relationship.Leg('cold', 10, exposure=0.1, recip=0.0),
        relationship.Leg('swing', 21, exposure=0.0, recip=0.0),
    ),
    enabled=True,
)
"""The curve both engines walk -- ONE arc, as config.apply_persona fans it."""

_CTRL = relationship.Control(wundt=attachment.WundtParams())
"""A plain control, for seeding a ledger: only its gap statistics are used."""


def _arc_bot(tmp_path: Path) -> main.Userbot:
    """Return a /who bot whose people are on a real arc, in both services.

    The shipped config puts the same ``persona.arc`` in both engine blocks,
    so a fixture that armed one of them would let the readout look right
    while the two services quietly walked different curves.
    """
    bot = _who_bot(tmp_path)
    bot.stories.clock = lambda: NOW
    bot.reactions.clock = lambda: NOW
    bot.stories.params = replace(bot.stories.params, arc=_ARC)
    bot.reactions.params = replace(bot.reactions.params, arc=_ARC)
    return bot


def test_both_services_put_a_person_in_the_same_leg(tmp_path: Path) -> None:
    """One person, one curve -- whichever engine is asked.

    The anchor is ``met()``, which is deliberately not bound to a service,
    and the arc is fanned into both engine blocks from ``persona``. Nothing
    asserted that the two ends actually meet, and they are what make the
    roster able to say one word about somebody.
    """
    bot = _arc_bot(tmp_path)
    for clock in (NOW, NOW + 18 * 86400.0):
        bot.stories.clock = bot.reactions.clock = lambda c=clock: c

        assert bot.stories.store.met(PEER_A) == bot.reactions.store.met(PEER_A)
        assert (
            bot.report._leg_of(PEER_A, 'stories').split(':')[0]
            == (bot.report._leg_of(PEER_A, 'reactions').split(':')[0])
        )


def test_a_person_row_says_what_we_are_doing_with_them_now(
    tmp_path: Path,
) -> None:
    """One word, before the percentages, and it follows the arc.

    The numbers are averages over everything that ever happened; the word is
    what is happening TODAY. Somebody eleven days into a cold shoulder reads
    as neglected in the percentages without them ever saying so.
    """
    bot = _arc_bot(tmp_path)
    warm = bot.report._doing('stories', PEER_A)  # met yesterday: honeymoon
    bot.stories.clock = lambda: NOW + 18 * 86400.0

    assert warm == 'like'
    assert bot.report._doing('stories', PEER_A) == 'ignore'


def test_the_roster_word_is_the_most_we_do_with_them_anywhere(
    tmp_path: Path,
) -> None:
    """Two services, one person, one word -- and the word is the strongest.

    Same leg, same day, different RECORD: we watch their stories and never
    answer, and we answer their comments. Reading one ledger called that
    "orbiting" and dropped the half where we actually talk to them.

    The two words also come from different ladders -- ``seen`` is not a rung
    the comment engine has, ``sticker`` is not one the story engine has --
    which is why the model counts rungs and the renderer names them.
    """
    bot, peer = _arc_bot(tmp_path), 7001  # ONE person, both ledgers
    bot.stories.ledger.add_take(peer, (1, 2, 3, 4), _CTRL, NOW)
    bot.reactions.ledger.add_take(peer, (1, 2, 3, 4), _CTRL, NOW)
    bot.reactions.ledger.bump_recip(peer, 1, NOW)

    assert bot.report._doing('stories', peer) == 'seen'
    assert bot.report._doing('reactions', peer) == 'sticker'
    assert bot.report.doing(peer) == 'sticker'  # the most, not the least


def test_the_plan_merges_both_engines_soonest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One queue, because the question is what the BOT is about to do.

    The comment reactions were listed with their etas and the planned story
    views were not listed at all -- they showed as a count in another
    section's header -- so a reader could see half the plan and never learn
    the other half existed. Interleaved by time, which is the only order
    that makes two queues one.
    """
    monkeypatch.setattr(time, 'time', lambda: NOW)
    bot = _bot(tmp_path)
    bot.reactions.state.pending = [
        replace(bot.reactions.state.pending[0], when=NOW + 900),
    ]
    bot.story_watch.pending = [
        stories.StoryView(PEER_B, (1, 2), 2, NOW + 60),
        stories.StoryView(PEER_C, (3,), 3, NOW + 1800),
    ]

    rows = _section(
        bot.report.text(_known({PEER_B: 'bob', PEER_C: 'carol'})), '[P]'
    )
    etas = re.findall(r'in ~(\d+)m', rows)

    assert etas == ['1', '15', '30']  # story, comment, story -- by the clock
    assert '2 stories @bob' in rows
    assert 'post 77' in rows  # and the comment side is still there


def test_the_plan_says_so_when_there_is_nothing_scheduled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty queue is a sentence, not a header over nothing."""
    monkeypatch.setattr(time, 'time', lambda: NOW)
    bot = _bot(tmp_path)
    bot.reactions.state.pending = []
    bot.story_watch.pending = []

    assert 'nothing scheduled' in _section(bot.report.text({}), '[P]')


def test_the_tag_names_the_service_that_earned_the_word(
    tmp_path: Path,
) -> None:
    """The roster says one word; the tag says which ledger it came from.

    The vocabularies do not overlap, but that is a mapping the reader would
    have to learn -- and the percentages beside the verb are the fold across
    both services either way.
    """
    bot, talker, watched = _arc_bot(tmp_path), 7101, 7102
    bot.reactions.ledger.add_take(talker, (1, 2, 3), _CTRL, NOW)
    bot.stories.ledger.add_take(watched, (1, 2, 3), _CTRL, NOW)

    assert bot.report._reading(talker)[0] == 'reactions'
    assert bot.report._reading(watched)[0] == 'stories'


def test_the_roster_names_somebody_we_only_ever_met_in_the_comments(
    tmp_path: Path,
) -> None:
    """The list of people is the union of the ledgers, not one service's.

    ``StateStore.peers`` answers per service by construction, so a commenter
    with no stories had a standing the roster could not see and was missing
    from the list of everybody -- which is the one thing that list is for.
    """
    bot, talker = _arc_bot(tmp_path), 7002
    bot.reactions.ledger.add_take(talker, (1, 2, 3), _CTRL, NOW)
    bot.reactions.ledger.bump_recip(talker, 1, NOW)
    bot.database('stories').note_actor(
        Actor(talker, 'user', username='talker')
    )

    got = bot.report.people()

    assert '@talker' in got
    assert 'sticker' in got  # in the comment ladder's words, not a story's


def test_the_doing_word_follows_the_config_not_the_leg_name(
    tmp_path: Path,
) -> None:
    """Retuning a leg moves the word, so it cannot become a stale label.

    Derived from what the controller is AIMED at rather than from the name
    in the JSON: a leg called "honeymoon" that stops answering is a leg we
    only watch, and the readout has to say so.
    """
    bot = _arc_bot(tmp_path)
    quiet = replace(
        bot.stories.params.arc.legs[0], recip=0.0
    )  # same name, no reciprocation
    bot.stories.params = replace(
        bot.stories.params,
        arc=replace(bot.stories.params.arc, legs=(quiet,)),
    )

    assert bot.report._doing('stories', PEER_A) == 'seen'


def test_people_lists_everyone_with_what_we_do_and_where_they_are(
    tmp_path: Path,
) -> None:
    """The middle view: /status is now, /who is one person, this is all.

    Ordered by how recently we touched somebody, so the top of the list is
    who the account is actually busy with.
    """
    got = _arc_bot(tmp_path).report.people()

    assert 'people, most recently touched first' in got
    assert got.splitlines()[1] == (
        '. @alice . stories . like . 0s . 71/20 . honeymoon 1'
    )


def test_a_roster_row_drops_a_field_that_has_nothing_to_say(
    tmp_path: Path,
) -> None:
    """Nothing offered anywhere means no ledger earned the word.

    Naming one would print the tie-break's arbitrary pick as a fact -- and
    say "comments" about somebody we have only ever seen post a story. The
    row is read across, so a missing field costs nothing and a wrong one
    costs the reader's trust in the column.
    """
    bot = _arc_bot(tmp_path)
    db = bot.database('stories')
    db.note_actor(Actor(7201, 'user', username='stranger'))
    # The shape the migration leaves for somebody an older file knew about
    # and we never acted on: a standing row of zeroes.
    db.conn.execute(
        'INSERT INTO standing (service, peer_id, offered, taken, recip,'
        ' last_at, take_at, gap_n, gap_sum, gap_sq, burst)'
        " VALUES ('stories', 7201, 0, 0, 0, 0, 0, 0, 0, 0, 0)"
    )
    db.conn.commit()
    row = next(
        line
        for line in bot.report.people().splitlines()
        if '@stranger' in line
    )

    assert row == '. @stranger . new . never . 0/0 . honeymoon 1'


def test_people_says_so_when_there_is_nobody(tmp_path: Path) -> None:
    """An empty roster is a sentence, not a header over nothing."""
    bot = _bot(tmp_path)
    bot.modes = SimpleNamespace(
        mode_of=bot.modes.mode_of, service_dir=lambda _name: tmp_path / 'e'
    )
    (tmp_path / 'e').mkdir()
    bot._dbs = {tmp_path / 'e': Database(tmp_path / 'e' / DB_NAME)}

    assert 'nobody yet' in bot.report.people()
