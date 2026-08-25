# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The aggregator's human-like cat-reply engine (minions/aggregator/cats.py).

Pure-logic tests: no Telethon, no network -- the engine is stdlib-only by
design, so every one of the nine behavioural principles is checked here.
"""

from __future__ import annotations

import random
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

from minions.aggregator.engines import cats

_BURST_SIZE = 6
_HALF = 0.5
_MIGRATED_AT = 555.0
_NEAR_ONE = 0.99
_SESSION_AT = 999.0
_THREAD_ROOT = 800
_TWO_CATS = 2
_WATCH_POSTS = 4

if TYPE_CHECKING:
    from pathlib import Path


def _ts(**over: object) -> object:
    base = {'year': 2026, 'month': 7, 'day': 15, 'hour': 12, 'minute': 0}
    base.update(over)
    return datetime(**base, tzinfo=UTC).timestamp()


def _params(**over: object) -> object:
    base = {
        'enabled': True,
        'like_all': False,
        'comments_in_discussion': False,
        'react_to_posts': False,
        'watch_posts': 4,
        'hours_weekday': ((12.0, 3.0, 1.0), (20.0, 3.0, 1.0)),
        'hours_weekend': ((12.0, 3.0, 1.0), (20.0, 3.0, 1.0)),
        'quiet_hours': frozenset({0, 1, 2, 3, 4, 5, 6}),
        'active_start': 0.0,
        'active_end': 24.0,
        'uptime_half_life_sec': 864000.0,
        'uptime_learn_obs': 2000.0,
        'skip_if_manually_replied': True,
        'tz_offset_hours': 0.0,
        'latency_log_mu': 3.0,
        'latency_log_sigma': 0.5,
        'spacing_log_mu': 3.0,
        'spacing_log_sigma': 0.5,
        'jitter_sec': 30.0,
        'skip_prob': 0.0,
        'double_prob': 0.0,
        'double_gap_sec': 40.0,
        'silent_day_prob': 0.0,
        'recency_half_life_sec': 1000.0,
        'mood_phi': 0.8,
        'mood_sigma': 0.3,
        'feedback_speedup': 0.4,
        'session_gap_log_mu': 3.0,
        'session_gap_log_sigma': 0.5,
        'session_idle_sec': 900.0,
        'session_max_sec': 1200.0,
        'max_reply_delay_sec': 86400.0,
        'pool': (
            cats.CatEmoji('1', 'a', 1.0, ('bodry',)),
            cats.CatEmoji('2', 'b', 1.0, ('sleepy',)),
        ),
        'like_pool': (
            cats.CatEmoji('L1', 'x', 1.0, ()),
            cats.CatEmoji('L2', 'y', 1.0, ()),
            cats.CatEmoji('L3', 'z', 1.0, ()),
        ),
        'sticker_gap': 6,
        'burst_count': 4,
        'burst_window_sec': 3600.0,
        'rescan_sec': 300.0,
    }
    base.update(over)
    return cats.CatParams(**base)


def _brain(tmp_path: Path, seed: int = 0, **over: object) -> object:
    brain = cats.CatBrain(
        _params(**over), tmp_path / 'cats_state.json', random.Random(seed)
    )
    brain.clock = _ts
    return brain


# --- principle 1: timing is a per-hour distribution, dead in the small hours


def test_density_is_zero_in_quiet_hours() -> None:
    """Check density is zero in quiet hours."""
    assert cats._density_weight(_ts(hour=3), _params()) == 0.0


def test_density_peaks_at_the_active_mean() -> None:
    """Check density peaks at the active mean."""
    params = _params()
    assert cats._density_weight(_ts(hour=12), params) > cats._density_weight(
        _ts(hour=16), params
    )


def test_weekday_and_weekend_curves_are_independent() -> None:
    # A weekend-only evening peak lifts Saturday above the flat weekday.
    """Check weekday and weekend curves are independent."""
    params = _params(
        hours_weekday=((12.0, 3.0, 1.0),),
        hours_weekend=((23.0, 1.0, 5.0),),
    )
    sat = _ts(year=2026, month=7, day=18, hour=23)  # Saturday
    wed = _ts(year=2026, month=7, day=15, hour=23)  # Wednesday
    assert cats._density_weight(sat, params) > cats._density_weight(
        wed, params
    )


# --- principle 2: heavy-tailed intervals


def test_lognormal_is_positive_and_heavy_tailed() -> None:
    """Check lognormal is positive and heavy tailed."""
    rng = random.Random(1)
    draws = [cats._lognormal(rng, 3.0, 1.0) for _ in range(2000)]
    assert all(d > 0 for d in draws)
    # A heavy tail: the max dwarfs the median (a uniform never would).
    draws.sort()
    assert draws[-1] > 8 * draws[len(draws) // 2]


# --- principle 3: selection has memory (recency penalty)


def test_just_used_cat_is_avoided(tmp_path: Path) -> None:
    """Check just used cat is avoided."""
    brain = _brain(
        tmp_path,
        pool=(
            cats.CatEmoji('fresh', 'f', 1.0, ('x',)),
            cats.CatEmoji('used', 'u', 1.0, ('x',)),
        ),
    )
    now = _ts()
    brain.state.cat_last = {'used': now}  # just sent -> suppressed
    picks = [brain._pick(now).emoji_id for _ in range(200)]
    assert picks.count('fresh') > picks.count('used') * 5


def test_favourite_base_is_chosen_more(tmp_path: Path) -> None:
    """Check favourite base is chosen more."""
    brain = _brain(
        tmp_path,
        pool=(
            cats.CatEmoji('fav', 'f', 6.0, ('x',)),
            cats.CatEmoji('rare', 'r', 1.0, ('x',)),
        ),
    )
    picks = [brain._pick(_ts()).emoji_id for _ in range(300)]
    assert picks.count('fav') > picks.count('rare')


# --- principle 4: latent mood, AR(1), drifts once a day


def test_mood_steps_once_per_day(tmp_path: Path) -> None:
    """Check mood steps once per day."""
    brain = _brain(tmp_path)
    brain._step_mood(_ts(day=15))
    first = brain.state.mood
    brain._step_mood(_ts(day=15, hour=20))  # same day: no change
    assert brain.state.mood == first
    brain._step_mood(_ts(day=16))  # new day: drifts
    assert brain.state.mood != first


# --- principle 5: context tags


def test_context_is_sleepy_in_the_morning() -> None:
    """Check context is sleepy in the morning."""
    assert 'sleepy' in cats._context_tags(_ts(hour=8), _params())
    assert 'bodry' in cats._context_tags(_ts(hour=20), _params())


def test_context_flags_december_as_holiday() -> None:
    """Check context flags december as holiday."""
    params = _params()
    assert 'newyear' in cats._context_tags(_ts(month=12, hour=13), params)
    assert 'newyear' not in cats._context_tags(_ts(month=7, hour=13), params)


# --- principle 6: jitter off the :00


def test_fire_time_lands_in_an_active_hour(tmp_path: Path) -> None:
    """Check fire time lands in an active hour."""
    brain = _brain(tmp_path)
    for _ in range(50):
        when = brain._fire_time(_ts(), engaged=False)
        hour = datetime.fromtimestamp(when, tz=UTC).hour
        assert hour not in brain.params.quiet_hours


def test_engaged_commenter_gets_a_faster_reaction(tmp_path: Path) -> None:
    # Same rng stream, so only the feedback speed-up differs.
    """Check engaged commenter gets a faster reaction."""
    plain = cats.CatBrain(_params(), tmp_path / 'a.json', random.Random(3))
    plain.clock = _ts
    eager = cats.CatBrain(_params(), tmp_path / 'b.json', random.Random(3))
    eager.clock = _ts
    # Compare the raw latency by disabling snapping noise via a huge peak.
    a = plain._fire_time(_ts(), engaged=False)
    b = eager._fire_time(_ts(), engaged=True)
    assert b <= a


# --- principle 7: built-in imperfection


def test_double_sends_a_second_cat(tmp_path: Path) -> None:
    """Check double sends a second cat."""
    brain = _brain(tmp_path, double_prob=1.0)
    assert len(brain.emit()) == _TWO_CATS


def test_skip_probability_drops_a_comment(tmp_path: Path) -> None:
    """Check skip probability drops a comment."""
    brain = _brain(tmp_path, skip_prob=1.0)
    assert brain.schedule('u', engaged=False) is None
    assert 'u' not in brain.state.catted  # a skip is not a "catted" person


def test_silent_day_yields_no_cat(tmp_path: Path) -> None:
    """Check silent day yields no cat."""
    brain = _brain(tmp_path, silent_day_prob=1.0)
    assert brain.schedule('u', engaged=False) is None


def test_like_all_bypasses_skip_and_silent_day(tmp_path: Path) -> None:
    """like_all likes every comment, even under a full skip / silent day."""
    brain = _brain(
        tmp_path, like_all=True, skip_prob=1.0, silent_day_prob=1.0
    )
    assert brain.schedule('u', engaged=False) is not None
    assert 'u' in brain.state.catted
    # Dedup still holds: the same key is not liked twice.
    assert brain.schedule('u', engaged=False) is None


# --- principle 2: sessions -- close comments share a burst, stale ones drop


def test_comments_close_together_share_one_burst(tmp_path: Path) -> None:
    """Check comments close together share one burst."""
    brain = _brain(tmp_path)
    first = brain.schedule('a', engaged=False)
    second = brain.schedule('b', engaged=False)
    assert first is not None
    assert second is not None
    # Same session: seconds apart, not smeared an hour apart by a cursor.
    assert abs(second - first) < brain.params.session_idle_sec


def test_a_comment_out_of_reach_goes_stale(tmp_path: Path) -> None:
    """Check a comment out of reach goes stale."""
    brain = _brain(
        tmp_path,
        active_start=7.0,
        active_end=17.0,
        quiet_hours=frozenset(),
        max_reply_delay_sec=3600.0,  # only 1h of reach
        uptime_learn_obs=2.0,
    )
    brain.clock = lambda: _ts(hour=20)  # host down; window 07:00 is >1h off
    assert brain.schedule('late', engaged=False) is None
    assert 'late' not in brain.state.catted  # stale, not a committed cat


# --- once-per-person, enabled gate


def test_a_person_is_catted_at_most_once(tmp_path: Path) -> None:
    """Check a person is catted at most once."""
    brain = _brain(tmp_path)
    assert brain.schedule('u', engaged=False) is not None
    assert brain.schedule('u', engaged=False) is None


def test_disabled_engine_never_schedules(tmp_path: Path) -> None:
    """Check disabled engine never schedules."""
    brain = _brain(tmp_path, enabled=False)
    assert brain.schedule('u', engaged=False) is None


# --- comment targeting: last N posts only


def test_is_comment_tracks_only_the_last_posts(tmp_path: Path) -> None:
    """Check is comment tracks only the last posts."""
    brain = _brain(tmp_path, watch_posts=4)
    brain.note_post(100, 5)
    assert brain.is_comment(100, 5)
    assert not brain.is_comment(100, 999)
    assert not brain.is_comment(100, None)
    for extra in range(10, 14):  # push 4 more -> post 5 falls off
        brain.note_post(100, extra)
    assert not brain.is_comment(100, 5)
    assert brain.is_comment(100, 13)


def test_catted_keys_are_pruned_when_a_post_rolls_off(tmp_path: Path) -> None:
    """Check catted keys are pruned when a post rolls off."""
    brain = _brain(tmp_path, watch_posts=2)
    brain.note_post(1, 10)
    assert brain.schedule('1:10:alice', engaged=False) is not None
    assert '1:10:alice' in brain.state.catted
    brain.note_post(1, 11)
    brain.note_post(1, 12)  # window is [11, 12] now -> post 10 rolled off
    assert '1:10:alice' not in brain.state.catted


# --- adaptive uptime: cold start follows the declared window, then learns


def _window_brain(tmp_path: Path) -> cats.CatBrain:
    brain = _brain(
        tmp_path,
        active_start=7.0,
        active_end=17.0,
        quiet_hours=frozenset(),
        hours_weekday=((12.0, 6.0, 1.0),),
        hours_weekend=((12.0, 6.0, 1.0),),
        uptime_learn_obs=2.0,  # trust the learned curve fast, for the test
    )
    brain.clock = _ts
    return brain


def test_cold_start_uptime_follows_the_declared_window(tmp_path: Path) -> None:
    """Check cold start uptime follows the declared window."""
    brain = _window_brain(tmp_path)
    assert brain._alive_fraction(_ts(hour=3)) == 0.0  # outside 7-17 prior
    assert brain._alive_fraction(_ts(hour=20)) == 0.0
    assert brain._alive_fraction(_ts(hour=12)) == 1.0  # inside the prior


def test_learned_uptime_adapts_beyond_the_declared_window(
    tmp_path: Path,
) -> None:
    """Check learned uptime adapts beyond the declared window."""
    brain = _window_brain(tmp_path)
    # The NAS is actually up at 20:00 (outside the 7-17 rule of thumb).
    brain.mark_alive(_ts(hour=20))
    brain.mark_alive(_ts(hour=20))
    brain.mark_alive(_ts(hour=20))
    assert brain._alive_fraction(_ts(hour=20)) > _HALF  # learned it is up then
    # And an hour it was never seen up decays toward zero.
    assert brain._alive_fraction(_ts(hour=12)) < _HALF


def test_effective_weight_gates_when_host_is_down(tmp_path: Path) -> None:
    """Check effective weight gates when host is down."""
    brain = _window_brain(tmp_path)
    assert brain._effective_weight(_ts(hour=3)) == 0.0  # host down (prior)
    assert brain._effective_weight(_ts(hour=12)) > 0.0  # host up + awake


# --- principle 9: watched posts and pending cats survive a restart


def test_watched_posts_survive_a_restart(tmp_path: Path) -> None:
    """Check watched posts survive a restart."""
    path = tmp_path / 'cats_state.json'
    brain = cats.CatBrain(_params(), path, random.Random(0))
    brain.note_post(100, 42)
    fresh = cats.CatBrain(_params(), path, random.Random(0))
    assert fresh.is_comment(100, 42)  # still watched after reload


def test_pending_cats_are_re_armed_and_missed_ones_renewed(
    tmp_path: Path,
) -> None:
    """Check pending cats are re armed and missed ones renewed."""
    path = tmp_path / 'cats_state.json'
    brain = cats.CatBrain(_params(), path, random.Random(0))
    brain.clock = _ts
    now = _ts()
    brain.add_pending(cats.Cat(5, 900, 900, now + 3600))  # future
    brain.add_pending(cats.Cat(5, 901, 901, now - 3600))  # missed
    fresh = cats.CatBrain(_params(), path, random.Random(0))
    fresh.clock = _ts
    armed = {c.reply_to: c.when for c in fresh.rearm()}
    assert armed[900] == now + 3600  # future one kept as-is
    assert armed[901] > now  # missed one renewed to the future


def test_pending_cat_emoji_round_trips(tmp_path: Path) -> None:
    # The cat chosen at schedule time is persisted and restored, so a
    # restart / requeue places (and /status shows) the SAME cat.
    """Check pending cat emoji round trips."""
    path = tmp_path / 'cats_state.json'
    brain = cats.CatBrain(_params(), path, random.Random(0))
    brain.add_pending(
        cats.Cat(5, 900, 900, 111.0, emojis=(('42', 'x'), ('43', 'y')))
    )
    fresh = cats.CatBrain(_params(), path, random.Random(0))
    (restored,) = fresh.rearm(renew_all=True)
    assert restored.emojis == (('42', 'x'), ('43', 'y'))


def test_done_pending_forgets_a_sent_cat(tmp_path: Path) -> None:
    """Check done pending forgets a sent cat."""
    brain = _brain(tmp_path)
    brain.add_pending(cats.Cat(5, 900, 900, 111.0))
    brain.add_pending(cats.Cat(5, 901, 901, 222.0))
    brain.done_pending(5, 900)
    assert [p['reply_to'] for p in brain.state.pending] == [901]


def test_due_now_sets_all_pending_to_now(tmp_path: Path) -> None:
    """Check due now sets all pending to now."""
    brain = _brain(tmp_path)
    brain.clock = _ts
    brain.add_pending(cats.Cat(5, 900, 800, _ts() + 99999))  # far future
    due = brain.due_now()
    assert len(due) == 1
    assert due[0].when == _ts()  # pulled back to now
    assert due[0].root == _THREAD_ROOT  # thread root preserved


# --- emit records the send


def test_emit_records_last_send_and_recency(tmp_path: Path) -> None:
    """Check emit records last send and recency."""
    brain = _brain(tmp_path)
    now = brain.clock()
    sent = brain.emit()
    assert len(sent) == 1
    assert brain.state.last_send == now
    assert brain.state.cat_last[sent[0].emoji_id] == now


def test_emit_with_empty_pool_sends_nothing(tmp_path: Path) -> None:
    """Check emit with empty pool sends nothing."""
    brain = _brain(tmp_path, pool=())
    assert brain.emit() == []


# --- the like/cat pools: weighted draw, seeded by the target key


def test_pick_like_returns_one_from_the_pool(tmp_path: Path) -> None:
    """Check pick like returns one from the pool."""
    brain = _brain(tmp_path)
    ids = {c.emoji_id for c in brain.params.like_pool}
    assert brain.pick_like('chat:5001')[0].emoji_id in ids


def test_pick_is_reproducible_for_equal_state(tmp_path: Path) -> None:
    # Weighted now, but still seeded by the key: two engines with the SAME
    # (fresh) state and rng pick the same emoji for the same key.
    """Check pick is reproducible for equal state."""
    a = cats.CatBrain(_params(), tmp_path / 'a.json', random.Random(0))
    b = cats.CatBrain(_params(), tmp_path / 'b.json', random.Random(0))
    a.clock = b.clock = _ts
    assert a.pick_like('k')[0].emoji_id == b.pick_like('k')[0].emoji_id
    assert a.pick_cat('k')[0].emoji_id == b.pick_cat('k')[0].emoji_id


def test_pick_avoids_repeats_within_a_burst(tmp_path: Path) -> None:
    # The main fix: each pick is recorded into cat_last (recency), so within a
    # burst (one frozen instant) an already-used emoji is suppressed -- with a
    # pool at least as large as the burst, every reaction is a different glyph.
    """Check pick avoids repeats within a burst."""
    pool = tuple(
        cats.CatEmoji(f'L{i}', chr(97 + i), 1.0, ()) for i in range(6)
    )
    brain = _brain(tmp_path, like_pool=pool)
    picks = [brain.pick_like(f'k{i}')[0].emoji_id for i in range(6)]
    assert len(set(picks)) == _BURST_SIZE  # no repeats across the burst


def test_pick_records_recency_and_persists(tmp_path: Path) -> None:
    """Check pick records recency and persists."""
    path = tmp_path / 'cats_state.json'
    brain = cats.CatBrain(_params(), path, random.Random(0))
    brain.clock = _ts
    chosen = brain.pick_like('k')[0].emoji_id
    assert brain.state.cat_last[chosen] == _ts()
    fresh = cats.CatBrain(_params(), path, random.Random(0))
    assert fresh.state.cat_last[chosen] == _ts()  # recency survived a restart


def test_pick_like_varies_across_keys(tmp_path: Path) -> None:
    """Check pick like varies across keys."""
    brain = _brain(tmp_path)
    seen = {brain.pick_like(f'k{i}')[0].emoji_id for i in range(50)}
    assert len(seen) > 1  # weighted draw: not the same like for every target


def test_pick_like_empty_pool_sends_nothing(tmp_path: Path) -> None:
    """Check pick like empty pool sends nothing."""
    brain = _brain(tmp_path, like_pool=())
    assert brain.pick_like('any') == []


# --- the sticker gate: deterministic, both conditions, per post


def test_pick_cat_returns_one_from_the_cat_pool(tmp_path: Path) -> None:
    """Check pick cat returns one from the cat pool."""
    brain = _brain(tmp_path)
    ids = {c.emoji_id for c in brain.params.pool}
    assert brain.pick_cat('chat:5001')[0].emoji_id in ids


def test_sticker_needs_both_silence_and_burst(tmp_path: Path) -> None:
    # gap=3, burst=3: the first 3 engagements build the silence; only once
    # BOTH the gap is met AND >=3 landed in the window does a sticker fire.
    """Check sticker needs both silence and burst."""
    brain = _brain(tmp_path, sticker_gap=3, burst_count=3)
    brain.clock = lambda: 1000.0  # all inside one burst window
    fires = [brain.should_sticker('c:1') for _ in range(5)]
    assert fires == [False, False, False, True, False]


def test_sticker_resets_the_silence_after_firing(tmp_path: Path) -> None:
    """Check sticker resets the silence after firing."""
    brain = _brain(tmp_path, sticker_gap=2, burst_count=1)
    brain.clock = lambda: 1000.0
    fires = [brain.should_sticker('c:1') for _ in range(6)]
    # every (gap+1)-th engagement fires, then the counter resets
    assert fires == [False, False, True, False, False, True]


def test_sticker_burst_window_expires(tmp_path: Path) -> None:
    # Spread engagements far apart: the burst never accumulates, so despite
    # plenty of silence, no sticker fires.
    """Check sticker burst window expires."""
    brain = _brain(
        tmp_path, sticker_gap=1, burst_count=3, burst_window_sec=100.0
    )
    now = [0.0]
    brain.clock = lambda: now[0]
    fired = False
    for _ in range(10):
        now[0] += 200.0  # each engagement is outside the previous window
        fired = fired or brain.should_sticker('c:1')
    assert fired is False


def test_sticker_gate_is_per_post(tmp_path: Path) -> None:
    """Check sticker gate is per post."""
    brain = _brain(tmp_path, sticker_gap=1, burst_count=1)
    brain.clock = lambda: 1000.0
    # post A accrues silence; post B is independent (its own counter)
    assert brain.should_sticker('A') is False
    assert brain.should_sticker('B') is False
    assert brain.should_sticker('A') is True  # A's 2nd engagement fires


def test_pending_kind_round_trips(tmp_path: Path) -> None:
    """Check pending kind round trips."""
    path = tmp_path / 'cats_state.json'
    brain = cats.CatBrain(_params(), path, random.Random(0))
    brain.add_pending(cats.Cat(5, 900, 900, 111.0, kind='reply'))
    fresh = cats.CatBrain(_params(), path, random.Random(0))
    (restored,) = fresh.rearm(renew_all=True)
    assert restored.kind == 'reply'


def test_load_reads_the_like_pool() -> None:
    """Check load reads the like pool."""
    params = cats.load_cat_params(
        {
            'cats': {'enabled': True},
            'emoji': [
                {'type': 'cat', 'id': '9', 'fallback': 'c'},
                {'type': 'like', 'id': '7', 'fallback': 'k'},
            ],
        }
    )
    assert [c.emoji_id for c in params.like_pool] == ['7']
    assert [c.emoji_id for c in params.pool] == ['9']


# --- principle 9 support: state persists across restarts


def test_state_round_trips_through_disk(tmp_path: Path) -> None:
    """Check state round trips through disk."""
    path = tmp_path / 'cats_state.json'
    brain = cats.CatBrain(_params(), path, random.Random(0))
    brain.state.mood = 0.5
    brain.state.catted = {'x', 'y'}
    brain.state.cat_last = {'1': 123.0}
    brain.state.next_session_at = 999.0
    brain._save()
    fresh = cats.CatBrain(_params(), path, random.Random(0))
    assert fresh.state.mood == _HALF
    assert fresh.state.catted == {'x', 'y'}
    assert fresh.state.cat_last == {'1': 123.0}
    assert fresh.state.next_session_at == _SESSION_AT


def test_corrupt_state_starts_fresh(tmp_path: Path) -> None:
    """Check corrupt state starts fresh."""
    path = tmp_path / 'cats_state.json'
    path.write_text('{ not json', encoding='utf-8')
    brain = cats.CatBrain(_params(), path, random.Random(0))
    assert brain.state.mood == 0.0
    assert brain.state.catted == set()


def test_old_state_migrates_next_earliest_to_session(tmp_path: Path) -> None:
    """Check old state migrates next earliest to session."""
    path = tmp_path / 'cats_state.json'
    path.write_text('{"next_earliest": 555.0}', encoding='utf-8')
    brain = cats.CatBrain(_params(), path, random.Random(0))
    assert (
        brain.state.next_session_at == _MIGRATED_AT
    )  # migrated from the old key


# --- loader


def test_load_cat_params_defaults_to_disabled() -> None:
    """Check load cat params defaults to disabled."""
    params = cats.load_cat_params({})
    assert params.enabled is False
    assert params.watch_posts == _WATCH_POSTS


def test_load_cat_params_reads_the_pool() -> None:
    # The cat pool is the type=cat entries of the unified top-level array.
    """Check load cat params reads the pool."""
    params = cats.load_cat_params(
        {
            'cats': {'enabled': True},
            'emoji': [
                {'type': 'love', 'id': '1', 'fallback': 'l'},
                {
                    'type': 'cat',
                    'id': '9',
                    'fallback': 'c',
                    'base': 2,
                    'tags': ['bodry'],
                },
            ],
        }
    )
    assert params.enabled is True
    assert len(params.pool) == 1  # only the type=cat entry
    assert params.pool[0].emoji_id == '9'
    assert params.pool[0].tags == ('bodry',)


# --- Berlyne attachment control on comment-likes (exposure + reciprocity) ---

_EXPOSURE_PEAK = 0.675  # argmax of the default Wundt curve (c1.45 c2.90 k8)
_RECIP_TARGET = 0.20
_CONVERGE_TOL = 0.05
_CONVERGE_N = 2000
_LIKE_CAP = 5
_STICKER_CAP = 2


def _no_caps(tmp_path: Path, seed: int = 0, **over: object) -> object:
    """Build a brain with the daily caps off, to isolate the control laws."""
    over.setdefault('like_max_per_day', 0)
    over.setdefault('sticker_max_per_day', 0)
    return _brain(tmp_path, seed, **over)


def test_decide_engage_always_likes_the_first_comment(tmp_path: Path) -> None:
    """A newcomer's very first comment is always engaged (a warm hello)."""
    brain = _no_caps(tmp_path)
    assert brain.decide_engage('newbie') is True
    assert brain.state.commented['newbie'] == 1
    assert brain.state.engaged['newbie'] == 1


def test_exposure_converges_to_the_wundt_peak(tmp_path: Path) -> None:
    """engaged/commented for a heavy commenter converges on ~0.67, not 1."""
    brain = _no_caps(tmp_path, seed=7)
    engaged = sum(brain.decide_engage('heavy') for _ in range(_CONVERGE_N))
    p = engaged / _CONVERGE_N
    assert abs(p - _EXPOSURE_PEAK) < _CONVERGE_TOL
    assert brain.state.commented['heavy'] == _CONVERGE_N


def test_reciprocity_converges_to_the_target(tmp_path: Path) -> None:
    """Among engaged comments, stickered/engaged converges on ~0.20."""
    brain = _no_caps(tmp_path, seed=3)
    engaged = 0
    stickered = 0
    for _ in range(_CONVERGE_N):
        if brain.decide_engage('fan'):
            engaged += 1
            if brain.decide_sticker('fan', content_ok=True):
                stickered += 1
    r = stickered / engaged
    assert abs(r - _RECIP_TARGET) < _CONVERGE_TOL


def test_a_steered_skip_is_recorded_not_re_rolled(tmp_path: Path) -> None:
    """A skipped comment bumps commented once and is never engaged later."""
    brain = _no_caps(tmp_path, seed=1)
    brain.decide_engage('p')  # first: always engaged
    # Drive p above the peak so the next draws are skips, then count.
    before = brain.state.commented['p']
    decisions = [brain.decide_engage('p') for _ in range(50)]
    assert brain.state.commented['p'] == before + 50  # each counted once
    assert brain.state.engaged['p'] <= 1 + sum(decisions)  # no phantom likes


def test_daily_like_cap_clamps_engagements(tmp_path: Path) -> None:
    """like_max_per_day caps total engagements in a day, no matter the flow."""
    brain = _brain(
        tmp_path, like_max_per_day=_LIKE_CAP, sticker_max_per_day=0
    )
    for _ in range(200):
        brain.decide_engage('spammer')
    assert brain.likes_today(_ts()) == _LIKE_CAP
    assert brain.state.engaged['spammer'] == _LIKE_CAP


def test_daily_sticker_cap_clamps_stickers(tmp_path: Path) -> None:
    """sticker_max_per_day caps the message-shaped stickers in a day."""
    brain = _brain(
        tmp_path,
        like_max_per_day=0,
        sticker_max_per_day=_STICKER_CAP,
        seed=3,
    )
    for _ in range(500):
        if brain.decide_engage('fan'):
            brain.decide_sticker('fan', content_ok=True)
    assert brain.stickers_today(_ts()) == _STICKER_CAP
    assert brain.state.stickered['fan'] == _STICKER_CAP


def test_a_question_comment_never_becomes_a_sticker(tmp_path: Path) -> None:
    """content_ok False keeps a plain like; the reciprocity roll is spared."""
    brain = _no_caps(tmp_path)
    brain.decide_engage('asker')  # engaged
    for _ in range(20):
        assert brain.decide_sticker('asker', content_ok=False) is False
    assert brain.state.stickered.get('asker', 0) == 0


def test_attachment_counters_persist_across_a_reload(tmp_path: Path) -> None:
    """commented/engaged/stickered survive a restart (relationship memory)."""
    path = tmp_path / 'cats_state.json'
    brain = cats.CatBrain(_params(like_max_per_day=0), path, random.Random(2))
    brain.clock = _ts
    for _ in range(30):
        if brain.decide_engage('mem'):
            brain.decide_sticker('mem', content_ok=True)
    saved = (
        brain.state.commented['mem'],
        brain.state.engaged['mem'],
        brain.state.stickered.get('mem', 0),
    )
    fresh = cats.CatBrain(_params(like_max_per_day=0), path, random.Random(2))
    assert fresh.state.commented['mem'] == saved[0]
    assert fresh.state.engaged['mem'] == saved[1]
    assert fresh.state.stickered.get('mem', 0) == saved[2]


def test_warmth_ranks_commenters_by_attachment_index(tmp_path: Path) -> None:
    """warmth() lists commenters warmest-first with p/r/index per person."""
    brain = _no_caps(tmp_path, seed=5)
    for _ in range(40):
        if brain.decide_engage('a'):
            brain.decide_sticker('a', content_ok=True)
    brain.decide_engage('b')  # a single, cold acquaintance
    warm = brain.warmth()
    assert {w.label for w in warm} == {'a', 'b'}
    assert warm == sorted(warm, key=lambda w: w.index, reverse=True)
