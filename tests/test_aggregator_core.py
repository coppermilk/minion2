# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Safety-net tests for the aggregator core and its pure helpers.

``main`` imports Telethon at module load, absent from the test extras, so a
handful of Telethon names are stubbed before importing. The Userbot flow
tests build the instance with ``object.__new__`` and wire only the few
attributes the method under test touches -- no live client.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from minions.userbot.core import config
from minions.userbot.core import matching
from minions.userbot.core import render
from minions.userbot.core import statefile
from minions.userbot.core.models import Config
from minions.userbot.core.models import Group
from minions.userbot.core.models import Item
from minions.userbot.core.models import Posted
from minions.userbot.glue import aggregator
from minions.userbot.glue.commands import CommandRouter
from minions.userbot.glue.profiles import ServiceModes

CONSTS = config.load_constants(config.CONSTANTS_PATH)


def _config(**over: object) -> Config:
    """Return a valid Config with the real defaults, overridable per test."""
    base: dict[str, object] = {
        'source': -1,
        'targets': (-2,),
        'test_target': 0,
        'platforms': ('tiktok', 'youtube', 'pinterest', 'instagram'),
        'threshold': 0.9,
        'timeout': 10800.0,
        'backfill': 100,
        'max_duration': 180,
        'repost_guard': 604800.0,
        'repost_guard_count': 5,
    }
    base.update(over)
    return Config(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------- validation


def test_validate_config_accepts_defaults() -> None:
    """A sane config passes validation (no exception)."""
    config._validate_config(_config())


@pytest.mark.parametrize(
    'over',
    [
        {'platforms': ()},
        {'threshold': 0.0},
        {'threshold': 1.5},
        {'timeout': -1.0},
        {'repost_guard_count': -1},
    ],
)
def test_validate_config_rejects_bad(over: dict[str, object]) -> None:
    """Each nonsensical knob fails fast with SystemExit."""
    with pytest.raises(SystemExit):
        config._validate_config(_config(**over))


# ---------------------------------------------------------------- matching


def test_parse_item_reads_fields() -> None:
    """A complete JSON object parses into an Item; platform lowercased."""
    text = (
        '{"action":"newVideo","platform":"YouTube",'
        '"caption":"Hi #x","link":"https://y/1","duration":"0:30"}'
    )
    msg_id = 7
    data = matching.extract_fields(text, CONSTS.fields.values())
    item = matching.parse_item(data, msg_id, CONSTS.fields)
    assert item is not None
    assert item.key == 'youtube'
    assert item.url == 'https://y/1'
    assert item.msg_id == msg_id


def test_parse_item_incomplete_is_none() -> None:
    """Missing platform/caption yields None (ignored, not a crash)."""
    data = matching.extract_fields('{"link":"https://y/1"}', ('link',))
    assert matching.parse_item(data, 1, CONSTS.fields) is None


@pytest.mark.parametrize(
    ('text', 'seconds'),
    [('0:30', 30), ('1:02:03', 3723), ('45', 45), ('', -1), ('nope', -1)],
)
def test_duration_seconds(text: str, seconds: int) -> None:
    """H:M:S / M:S / S parse; unknown or garbage is -1."""
    assert matching.duration_seconds(text) == seconds


def test_needs_human_flags_questions_and_links() -> None:
    """A question or a link wants a real reply, plain wording does not."""
    assert matching.needs_human('is this ok?', ())
    assert matching.needs_human('see https://x', ())
    assert not matching.needs_human('nice one', ())


def test_similar_merges_a_longer_caption_of_the_same_video() -> None:
    """A short caption that is a prefix of a longer one is the same video.

    This is the "combining did not work" bug: one platform's caption carried a
    second sentence, so the plain ratio fell under the threshold and the video
    split into two half-collected groups. (ASCII stand-in for a real caption,
    per the repo-wide ASCII source gate.)
    """
    short = matching.norm('you can choose not to believe in yourself')
    long = matching.norm(
        'you can choose not to believe in yourself, neville will be proud'
    )
    assert matching.similar(short, long) == 1.0  # prefix -> same video
    assert matching.similar(long, short) == 1.0  # order-independent


def test_similar_does_not_merge_a_short_generic_prefix() -> None:
    """A prefix under the min length is too generic to force a match."""
    assert matching.similar('no', 'no way that cannot be true') < 0.9  # noqa: PLR2004


def test_similar_keeps_ratio_for_unrelated_titles() -> None:
    """Unrelated captions stay well below the match threshold."""
    assert (
        matching.similar(
            matching.norm('you can choose not to believe in yourself'),
            matching.norm('a completely different video about cats'),
        )
        < 0.9  # noqa: PLR2004
    )


# ------------------------------------------------------------------ render


def test_col_widths_tracks_longest_per_column() -> None:
    """Column widths are the longest label seen in each column."""
    rows = [[('a', 'View'), ('b', 'Open')], [('c', 'Watch')]]
    assert render._col_widths(rows) == {0: 5, 1: 4}


def test_strip_tags_drops_hashtags() -> None:
    """Trailing hashtags are removed for display."""
    assert render._strip_tags('Salsa dance #a #b') == 'Salsa dance'


def test_youtube_thumb_only_from_youtube() -> None:
    """The thumbnail is taken from the YouTube item only."""
    yt = Item('youtube', 'youtube', 't', 'u', 'thumb.jpg', '', 1)
    assert render.youtube_thumb(Group('t', {'youtube': yt})) == 'thumb.jpg'
    assert render.youtube_thumb(Group('t', {})) == ''


# --------------------------------------------------------------- statefile


def test_posted_round_trip() -> None:
    """A Posted record survives dict serialization unchanged."""
    post = Posted('T', '2026-08-20T15:05:21Z', {'yt': 'u'}, [1, 2])
    back = statefile.posted_from_dict(statefile.posted_dict(post))
    assert back == post


def test_pending_round_trip_keeps_items_and_time() -> None:
    """A pending Group survives dict serialization (items, ids, created_at)."""
    item = Item('tiktok', 'tiktok', 'T', 'u', '', '30', 9)
    group = Group('T', {'tiktok': item}, {9}, created_at=1_700_000_000.0)
    back = statefile.pending_from_dict(
        statefile.pending_dict(group, ('tiktok', 'youtube'))
    )
    assert back.title == 'T'
    assert back.msg_ids == {9}
    assert back.items['tiktok'].url == 'u'
    assert back.created_at == group.created_at


def test_read_state_degrades_to_empty(tmp_path: Path) -> None:
    """Missing, unreadable and not-an-object all read as "no state"."""
    assert statefile.read_state(tmp_path / 'absent.json') == {}
    bad = tmp_path / 'bad.json'
    bad.write_text('{oops', encoding='utf-8')
    assert statefile.read_state(bad) == {}
    listed = tmp_path / 'list.json'
    listed.write_text('[1, 2]', encoding='utf-8')
    assert statefile.read_state(listed) == {}


def test_write_state_round_trips_and_leaves_no_temp(tmp_path: Path) -> None:
    """The write lands whole, keeps non-ASCII, and cleans up after itself."""
    path = tmp_path / 'state.json'
    statefile.write_state(path, {'name': '\u0448\u043a\u0430\u0444'})
    assert statefile.read_state(path) == {'name': '\u0448\u043a\u0430\u0444'}
    assert not (tmp_path / 'state.tmp').exists()


def test_write_state_keeps_the_old_file_when_the_move_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kill between writing and moving leaves the old state whole (CT-A).

    The watchdog turns a hang into a hard ``os._exit(1)``, so a write
    interrupted part-way is a case that happens. Injecting a failure at the
    ``replace`` proves the new bytes land in a sibling temp file first --
    the previous state is never the thing being overwritten.
    """
    path = tmp_path / 'state.json'
    statefile.write_state(path, {'n': 1})

    def boom(self: Path, target: Path) -> Path:
        msg = f'interrupted moving {self} onto {target}'
        raise OSError(msg)

    monkeypatch.setattr(Path, 'replace', boom)
    with pytest.raises(OSError, match='interrupted moving'):
        statefile.write_state(path, {'n': 2})
    assert statefile.read_state(path) == {'n': 1}


# ------------------------------------------------------- Userbot core flow


def _bare_core() -> aggregator.LinkAggregator:
    """Return a poster with just the core-flow collaborators wired.

    A plain constructor call: the poster no longer needs a Telethon client
    to exist. Only the two side effects the flow would run -- persisting and
    arming a timeout -- are stubbed out.
    """
    agg = aggregator.LinkAggregator(
        aggregator.AggregatorDeps(
            account=None,
            config=_config(),
            consts=CONSTS,
            state_path=None,
            targets=tuple,
            on_posted=None,
            field_keys=tuple(CONSTS.fields.values()),
            variety=None,
        )
    )
    agg._save = lambda: None
    agg._arm = lambda _group: None
    return agg


def test_recently_posted_blocks_same_title() -> None:
    """A title matching a posted record is a re-post; a new one is not."""
    agg = _bare_core()
    agg.posted = [Posted('Salsa dance', '2026-08-22T00:00:00Z', {}, [])]
    assert agg._recently_posted('Salsa dance #x')
    assert not agg._recently_posted('Totally different clip')


def test_group_for_skips_recent_repost() -> None:
    """_group_for returns None for a title already posted in-window."""
    agg = _bare_core()
    agg.posted = [Posted('Salsa dance', '2026-08-22T00:00:00Z', {}, [])]
    item = Item('tiktok', 'tiktok', 'Salsa dance #x', 'u', '', '', 5)
    assert agg._group_for(item) is None


def test_group_for_starts_new_group() -> None:
    """A fresh title starts (and returns) a new in-flight group."""
    agg = _bare_core()
    item = Item('tiktok', 'tiktok', 'Brand new clip', 'u', '', '', 5)
    group = agg._group_for(item)
    assert group is not None
    assert group in agg.groups


def test_short_or_reject_drops_long_video() -> None:
    """A video at/over max_duration is rejected and remembered."""
    agg = _bare_core()
    item = Item('tiktok', 'tiktok', 'Long one', 'u', '', '5:00', 5)
    assert agg._short_or_reject(item, 5) is None
    assert matching.norm('Long one') in agg.rejected


# ------------------------------------------------------ per-service modes


def _modes(tmp_path: Path, settings: dict[str, object]) -> ServiceModes:
    """Build a ServiceModes over a stub bot carrying only its settings."""
    bot = SimpleNamespace(settings=settings)
    return ServiceModes(bot, tmp_path)


def test_default_mode_follows_json_enabled(tmp_path: Path) -> None:
    """The poster defaults live; a feature only if its JSON says enabled."""
    modes = _modes(
        tmp_path,
        {
            'engines': {
                'reactions': {'enabled': True},
                'stories': {'enabled': False},
            }
        },
    )
    assert modes.mode_of('aggregator') == 'live'
    assert modes.mode_of('reactions') == 'live'
    assert modes.mode_of('stories') == 'off'


def test_stored_modes_are_read_and_junk_falls_to_the_default(
    tmp_path: Path,
) -> None:
    """The stored services block is read; a junk value falls to the default."""
    (tmp_path / 'aggregator_mode.json').write_text(
        json.dumps(
            {
                'services': {
                    'aggregator': 'test',
                    'reactions': 'off',
                    'stories': 'live',
                    'users': 'bogus',
                    'greeter': 'off',
                }
            }
        )
    )
    modes = _modes(tmp_path, {'engines': {'reactions': {'enabled': True}}})
    assert modes.mode_of('aggregator') == 'test'
    assert modes.mode_of('reactions') == 'off'
    assert modes.mode_of('users') == 'off'  # 'bogus' -> the users default


def test_modes_fall_back_when_the_file_is_absent(tmp_path: Path) -> None:
    """No modes file (a fresh install) -> every service on its own default."""
    modes = _modes(
        tmp_path,
        {
            'engines': {
                'reactions': {'enabled': True},
                'users': {'enabled': False},
            }
        },
    )
    assert modes.mode_of('aggregator') == 'live'  # the poster is always live
    assert modes.mode_of('reactions') == 'live'  # JSON-enabled -> live
    assert modes.mode_of('users') == 'off'  # JSON-disabled -> off


def test_enabled_is_mode_not_off(tmp_path: Path) -> None:
    """A service counts as enabled unless its mode is 'off'."""
    modes = _modes(tmp_path, {})
    modes.by_service = {
        'reactions': 'test',
        'stories': 'off',
        'greeter': 'live',
    }
    assert modes.enabled('reactions') is True
    assert modes.enabled('greeter') is True
    assert modes.enabled('stories') is False


def test_service_dir_follows_each_services_mode(tmp_path: Path) -> None:
    """A test service lands in base/test; a live one in base."""
    modes = _modes(tmp_path, {})
    modes.by_service = {'aggregator': 'live', 'reactions': 'test'}
    assert modes.service_dir('aggregator') == tmp_path
    assert modes.service_dir('reactions') == tmp_path / 'test'


def _router_with_label(label: str) -> CommandRouter:
    """Return a router whose reaction engine carries a persona label."""
    bot = SimpleNamespace(
        reactions=SimpleNamespace(params=SimpleNamespace(label=label))
    )
    return CommandRouter(bot)


def test_reaction_alias_maps_persona_label_to_canonical() -> None:
    """A label answers /<label>now and /<label>_<action>; none = neutral."""
    router = _router_with_label('cat')
    assert router._reaction_alias('/catnow') == '/reactnow'
    assert router._reaction_alias('/cat_on') == '/reactions_on'
    assert router._reaction_alias('/cat_test') == '/reactions_test'
    assert router._reaction_alias('/status') == '/status'
    # no label configured -> friendly names are not recognised, text is as-is
    assert _router_with_label('')._reaction_alias('/catnow') == '/catnow'
