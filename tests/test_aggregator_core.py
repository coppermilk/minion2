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
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from tests.conftest import install_telethon_stub

if TYPE_CHECKING:
    from pathlib import Path

install_telethon_stub()

from minions.userbot import main  # noqa: E402
from minions.userbot.core import config  # noqa: E402
from minions.userbot.core import matching  # noqa: E402
from minions.userbot.core import render  # noqa: E402
from minions.userbot.core import statefile  # noqa: E402
from minions.userbot.core.models import Config  # noqa: E402
from minions.userbot.core.models import Group  # noqa: E402
from minions.userbot.core.models import Item  # noqa: E402
from minions.userbot.core.models import Posted  # noqa: E402

CONSTS = config._load_constants(config.CONSTANTS_PATH)


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
        'discussion_gap': 0.0,
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
    data = matching._extract_fields(text, CONSTS.fields.values())
    item = matching._parse_item(data, msg_id, CONSTS.fields)
    assert item is not None
    assert item.key == 'youtube'
    assert item.url == 'https://y/1'
    assert item.msg_id == msg_id


def test_parse_item_incomplete_is_none() -> None:
    """Missing platform/caption yields None (ignored, not a crash)."""
    data = matching._extract_fields('{"link":"https://y/1"}', ('link',))
    assert matching._parse_item(data, 1, CONSTS.fields) is None


@pytest.mark.parametrize(
    ('text', 'seconds'),
    [('0:30', 30), ('1:02:03', 3723), ('45', 45), ('', -1), ('nope', -1)],
)
def test_duration_seconds(text: str, seconds: int) -> None:
    """H:M:S / M:S / S parse; unknown or garbage is -1."""
    assert matching._duration_seconds(text) == seconds


def test_needs_human_flags_questions_and_links() -> None:
    """A question or a link wants a real reply, plain wording does not."""
    assert matching._needs_human('is this ok?', ())
    assert matching._needs_human('see https://x', ())
    assert not matching._needs_human('nice one', ())


def test_similar_merges_a_longer_caption_of_the_same_video() -> None:
    """A short caption that is a prefix of a longer one is the same video.

    This is the "combining did not work" bug: one platform's caption carried a
    second sentence, so the plain ratio fell under the threshold and the video
    split into two half-collected groups. (ASCII stand-in for a real caption,
    per the repo-wide ASCII source gate.)
    """
    short = matching._norm('you can choose not to believe in yourself')
    long = matching._norm(
        'you can choose not to believe in yourself, neville will be proud'
    )
    assert matching._similar(short, long) == 1.0  # prefix -> same video
    assert matching._similar(long, short) == 1.0  # order-independent


def test_similar_does_not_merge_a_short_generic_prefix() -> None:
    """A prefix under the min length is too generic to force a match."""
    assert matching._similar('no', 'no way that cannot be true') < 0.9  # noqa: PLR2004


def test_similar_keeps_ratio_for_unrelated_titles() -> None:
    """Unrelated captions stay well below the match threshold."""
    assert (
        matching._similar(
            matching._norm('you can choose not to believe in yourself'),
            matching._norm('a completely different video about cats'),
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
    assert render._youtube_thumb(Group('t', {'youtube': yt})) == 'thumb.jpg'
    assert render._youtube_thumb(Group('t', {})) == ''


# --------------------------------------------------------------- statefile


def test_posted_round_trip() -> None:
    """A Posted record survives dict serialization unchanged."""
    post = Posted('T', '2026-08-20T15:05:21Z', {'yt': 'u'}, [1, 2])
    back = statefile._posted_from_dict(statefile._posted_dict(post))
    assert back == post


def test_pending_round_trip_keeps_items_and_time() -> None:
    """A pending Group survives dict serialization (items, ids, created_at)."""
    item = Item('tiktok', 'tiktok', 'T', 'u', '', '30', 9)
    group = Group('T', {'tiktok': item}, {9}, created_at=1_700_000_000.0)
    back = statefile._pending_from_dict(
        statefile._pending_dict(group, ('tiktok', 'youtube'))
    )
    assert back.title == 'T'
    assert back.msg_ids == {9}
    assert back.items['tiktok'].url == 'u'
    assert back.created_at == group.created_at


# ------------------------------------------------------- Userbot core flow


def _bare_core() -> main.Userbot:
    """Return an Userbot with just the core-flow collaborators wired."""
    agg = object.__new__(main.Userbot)
    agg.config = _config()
    agg.consts = CONSTS
    agg.groups = []
    agg.posted = []
    agg.rejected = set()
    agg.processed_ids = set()
    agg._keys = tuple(CONSTS.fields.values())
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
    assert matching._norm('Long one') in agg.rejected


# ------------------------------------------------------ per-service modes


def test_default_mode_follows_json_enabled() -> None:
    """Userbot defaults live; a feature is live only if its JSON is on."""
    agg = object.__new__(main.Userbot)
    agg._raw = {'reactions': {'enabled': True}, 'stories': {'enabled': False}}
    assert agg._default_mode('aggregator') == 'live'
    assert agg._default_mode('reactions') == 'live'
    assert agg._default_mode('stories') == 'off'


def test_migrate_service_modes_from_legacy_global(tmp_path: Path) -> None:
    """A pre-per-service install seeds from the old global mode + overrides."""
    agg = object.__new__(main.Userbot)
    agg._raw = {
        'reactions': {'enabled': True},
        'stories': {'enabled': True},
        'users': {'enabled': False},
        'greeter': {'enabled': False},
    }
    agg._overrides_path = tmp_path / 'absent.json'  # no legacy overrides
    modes = agg._migrate_service_modes({'mode': 'test'})
    assert modes['aggregator'] == 'test'  # poster always followed the mode
    assert modes['reactions'] == 'test'  # on -> the legacy mode
    assert modes['users'] == 'off'  # disabled -> off


def test_load_service_modes_reads_and_cleans_the_block(
    tmp_path: Path,
) -> None:
    """The stored services block is read; a junk value falls to the default."""
    agg = object.__new__(main.Userbot)
    agg._raw = {'reactions': {'enabled': True}}
    agg._mode_path = tmp_path / 'mode.json'
    agg._overrides_path = tmp_path / 'ov.json'
    agg._mode_path.write_text(
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
    modes = agg._load_service_modes()
    assert modes['aggregator'] == 'test'
    assert modes['reactions'] == 'off'
    assert modes['users'] == 'off'  # 'bogus' -> users default (JSON off)


def test_load_service_modes_migrates_the_legacy_cats_key(
    tmp_path: Path,
) -> None:
    """A pre-rename 'cats' service key is carried over to 'reactions'."""
    agg = object.__new__(main.Userbot)
    agg._raw = {'reactions': {'enabled': True}}
    agg._mode_path = tmp_path / 'mode.json'
    agg._overrides_path = tmp_path / 'ov.json'
    agg._mode_path.write_text(
        json.dumps({'services': {'aggregator': 'live', 'cats': 'test'}})
    )
    modes = agg._load_service_modes()
    assert modes['reactions'] == 'test'  # the old 'cats' mode is preserved


def test_migrate_reaction_state_renames_the_legacy_file(
    tmp_path: Path,
) -> None:
    """cats_state.json moves to reactions_state.json once, never clobbered."""
    (tmp_path / 'cats_state.json').write_text('{"mood": 1}')
    main.Userbot._migrate_reaction_state(tmp_path)
    assert not (tmp_path / 'cats_state.json').exists()
    assert (tmp_path / 'reactions_state.json').read_text() == '{"mood": 1}'
    # when the new file already exists, a stale old one is left untouched
    (tmp_path / 'cats_state.json').write_text('{"mood": 9}')
    main.Userbot._migrate_reaction_state(tmp_path)
    assert (tmp_path / 'reactions_state.json').read_text() == '{"mood": 1}'


def _agg_with_label(label: str) -> object:
    """Build a bare Userbot whose reaction engine carries a persona label."""
    agg = object.__new__(main.Userbot)
    agg.reactions = SimpleNamespace(params=SimpleNamespace(label=label))
    return agg


def test_reaction_alias_maps_persona_label_to_canonical() -> None:
    """A label answers /<label>now and /<label>_<action>; none = neutral."""
    agg = _agg_with_label('cat')
    assert main.Userbot._reaction_alias(agg, '/catnow') == '/reactnow'
    assert main.Userbot._reaction_alias(agg, '/cat_on') == '/reactions_on'
    assert main.Userbot._reaction_alias(agg, '/cat_test') == '/reactions_test'
    assert main.Userbot._reaction_alias(agg, '/status') == '/status'
    # no label configured -> friendly names are not recognised, text is as-is
    plain = _agg_with_label('')
    assert main.Userbot._reaction_alias(plain, '/catnow') == '/catnow'


def test_feature_enabled_is_mode_not_off() -> None:
    """A service counts as enabled unless its mode is 'off'."""
    agg = object.__new__(main.Userbot)
    agg._modes = {'reactions': 'test', 'stories': 'off', 'greeter': 'live'}
    assert agg._feature_enabled('reactions') is True
    assert agg._feature_enabled('greeter') is True
    assert agg._feature_enabled('stories') is False


def test_service_dir_follows_each_services_mode(tmp_path: Path) -> None:
    """A test service lands in base/test; a live one in base."""
    agg = object.__new__(main.Userbot)
    agg._state_base = tmp_path
    agg._modes = {'aggregator': 'live', 'reactions': 'test'}
    assert agg._service_dir('aggregator') == tmp_path
    assert agg._service_dir('reactions') == tmp_path / 'test'
