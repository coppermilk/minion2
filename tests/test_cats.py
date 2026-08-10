"""The aggregator's human-like cat-reply engine (minions/aggregator/cats.py).

Pure-logic tests: no Telethon, no network -- the engine is stdlib-only by
design, so every one of the nine behavioural principles is checked here.
"""

from __future__ import annotations

import random
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

from minions.aggregator import cats

if TYPE_CHECKING:
    from pathlib import Path


def _ts(**over):
    base = {'year': 2026, 'month': 7, 'day': 15, 'hour': 12, 'minute': 0}
    base.update(over)
    return datetime(**base, tzinfo=UTC).timestamp()


def _params(**over):
    base = {
        'enabled': True,
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


def _brain(tmp_path, seed=0, **over):
    brain = cats.CatBrain(
        _params(**over), tmp_path / 'cats_state.json', random.Random(seed)
    )
    brain.clock = _ts
    return brain


# --- principle 1: timing is a per-hour distribution, dead in the small hours


def test_density_is_zero_in_quiet_hours() -> None:
    assert cats._density_weight(_ts(hour=3), _params()) == 0.0


def test_density_peaks_at_the_active_mean() -> None:
    params = _params()
    assert cats._density_weight(_ts(hour=12), params) > cats._density_weight(
        _ts(hour=16), params
    )


def test_weekday_and_weekend_curves_are_independent() -> None:
    # A weekend-only evening peak lifts Saturday above the flat weekday.
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
    rng = random.Random(1)
    draws = [cats._lognormal(rng, 3.0, 1.0) for _ in range(2000)]
    assert all(d > 0 for d in draws)
    # A heavy tail: the max dwarfs the median (a uniform never would).
    draws.sort()
    assert draws[-1] > 8 * draws[len(draws) // 2]


# --- principle 3: selection has memory (recency penalty)


def test_recency_penalty_suppresses_then_recovers() -> None:
    assert cats._recency_penalty(0.0, 1000.0) == 0.0
    assert cats._recency_penalty(500.0, 1000.0) < cats._recency_penalty(
        5000.0, 1000.0
    )
    assert cats._recency_penalty(1e9, 1000.0) > 0.99


def test_just_used_cat_is_avoided(tmp_path: Path) -> None:
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
    brain = _brain(tmp_path)
    brain._step_mood(_ts(day=15))
    first = brain.state.mood
    brain._step_mood(_ts(day=15, hour=20))  # same day: no change
    assert brain.state.mood == first
    brain._step_mood(_ts(day=16))  # new day: drifts
    assert brain.state.mood != first


# --- principle 5: context tags


def test_context_is_sleepy_in_the_morning() -> None:
    assert 'sleepy' in cats._context_tags(_ts(hour=8), _params())
    assert 'bodry' in cats._context_tags(_ts(hour=20), _params())


def test_context_flags_december_as_holiday() -> None:
    params = _params()
    assert 'newyear' in cats._context_tags(_ts(month=12, hour=13), params)
    assert 'newyear' not in cats._context_tags(_ts(month=7, hour=13), params)


# --- principle 6: jitter off the :00


def test_fire_time_lands_in_an_active_hour(tmp_path: Path) -> None:
    brain = _brain(tmp_path)
    for _ in range(50):
        when = brain._fire_time(_ts(), engaged=False)
        hour = datetime.fromtimestamp(when, tz=UTC).hour
        assert hour not in brain.params.quiet_hours


def test_engaged_commenter_gets_a_faster_reaction(tmp_path: Path) -> None:
    # Same rng stream, so only the feedback speed-up differs.
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
    brain = _brain(tmp_path, double_prob=1.0)
    assert len(brain.emit()) == 2


def test_skip_probability_drops_a_comment(tmp_path: Path) -> None:
    brain = _brain(tmp_path, skip_prob=1.0)
    assert brain.schedule('u', engaged=False) is None
    assert 'u' not in brain.state.catted  # a skip is not a "catted" person


def test_silent_day_yields_no_cat(tmp_path: Path) -> None:
    brain = _brain(tmp_path, silent_day_prob=1.0)
    assert brain.schedule('u', engaged=False) is None


# --- principle 2: sessions -- close comments share a burst, stale ones drop


def test_comments_close_together_share_one_burst(tmp_path: Path) -> None:
    brain = _brain(tmp_path)
    first = brain.schedule('a', engaged=False)
    second = brain.schedule('b', engaged=False)
    assert first is not None
    assert second is not None
    # Same session: seconds apart, not smeared an hour apart by a cursor.
    assert abs(second - first) < brain.params.session_idle_sec


def test_a_comment_out_of_reach_goes_stale(tmp_path: Path) -> None:
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
    brain = _brain(tmp_path)
    assert brain.schedule('u', engaged=False) is not None
    assert brain.schedule('u', engaged=False) is None


def test_disabled_engine_never_schedules(tmp_path: Path) -> None:
    brain = _brain(tmp_path, enabled=False)
    assert brain.schedule('u', engaged=False) is None


# --- comment targeting: last N posts only


def test_is_comment_tracks_only_the_last_posts(tmp_path: Path) -> None:
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
    brain = _window_brain(tmp_path)
    assert brain._alive_fraction(_ts(hour=3)) == 0.0  # outside 7-17 prior
    assert brain._alive_fraction(_ts(hour=20)) == 0.0
    assert brain._alive_fraction(_ts(hour=12)) == 1.0  # inside the prior


def test_learned_uptime_adapts_beyond_the_declared_window(
    tmp_path: Path,
) -> None:
    brain = _window_brain(tmp_path)
    # The NAS is actually up at 20:00 (outside the 7-17 rule of thumb).
    brain.mark_alive(_ts(hour=20))
    brain.mark_alive(_ts(hour=20))
    brain.mark_alive(_ts(hour=20))
    assert brain._alive_fraction(_ts(hour=20)) > 0.5  # learned it is up then
    # And an hour it was never seen up decays toward zero.
    assert brain._alive_fraction(_ts(hour=12)) < 0.5


def test_effective_weight_gates_when_host_is_down(tmp_path: Path) -> None:
    brain = _window_brain(tmp_path)
    assert brain._effective_weight(_ts(hour=3)) == 0.0  # host down (prior)
    assert brain._effective_weight(_ts(hour=12)) > 0.0  # host up + awake


# --- principle 9: watched posts and pending cats survive a restart


def test_watched_posts_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / 'cats_state.json'
    brain = cats.CatBrain(_params(), path, random.Random(0))
    brain.note_post(100, 42)
    fresh = cats.CatBrain(_params(), path, random.Random(0))
    assert fresh.is_comment(100, 42)  # still watched after reload


def test_pending_cats_are_re_armed_and_missed_ones_renewed(
    tmp_path: Path,
) -> None:
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
    path = tmp_path / 'cats_state.json'
    brain = cats.CatBrain(_params(), path, random.Random(0))
    brain.add_pending(
        cats.Cat(5, 900, 900, 111.0, emojis=(('42', 'x'), ('43', 'y')))
    )
    fresh = cats.CatBrain(_params(), path, random.Random(0))
    (restored,) = fresh.rearm(renew_all=True)
    assert restored.emojis == (('42', 'x'), ('43', 'y'))


def test_done_pending_forgets_a_sent_cat(tmp_path: Path) -> None:
    brain = _brain(tmp_path)
    brain.add_pending(cats.Cat(5, 900, 900, 111.0))
    brain.add_pending(cats.Cat(5, 901, 901, 222.0))
    brain.done_pending(5, 900)
    assert [p['reply_to'] for p in brain.state.pending] == [901]


def test_due_now_sets_all_pending_to_now(tmp_path: Path) -> None:
    brain = _brain(tmp_path)
    brain.clock = _ts
    brain.add_pending(cats.Cat(5, 900, 800, _ts() + 99999))  # far future
    due = brain.due_now()
    assert len(due) == 1
    assert due[0].when == _ts()  # pulled back to now
    assert due[0].root == 800  # thread root preserved


# --- emit records the send


def test_emit_records_last_send_and_recency(tmp_path: Path) -> None:
    brain = _brain(tmp_path)
    now = brain.clock()
    sent = brain.emit()
    assert len(sent) == 1
    assert brain.state.last_send == now
    assert brain.state.cat_last[sent[0].emoji_id] == now


def test_emit_with_empty_pool_sends_nothing(tmp_path: Path) -> None:
    brain = _brain(tmp_path, pool=())
    assert brain.emit() == []


# --- the like/cat pools: weighted draw, seeded by the target key


def test_pick_like_returns_one_from_the_pool(tmp_path: Path) -> None:
    brain = _brain(tmp_path)
    ids = {c.emoji_id for c in brain.params.like_pool}
    assert brain.pick_like('chat:5001')[0].emoji_id in ids


def test_pick_is_reproducible_for_equal_state(tmp_path: Path) -> None:
    # Weighted now, but still seeded by the key: two engines with the SAME
    # (fresh) state and rng pick the same emoji for the same key.
    a = cats.CatBrain(_params(), tmp_path / 'a.json', random.Random(0))
    b = cats.CatBrain(_params(), tmp_path / 'b.json', random.Random(0))
    a.clock = b.clock = _ts
    assert a.pick_like('k')[0].emoji_id == b.pick_like('k')[0].emoji_id
    assert a.pick_cat('k')[0].emoji_id == b.pick_cat('k')[0].emoji_id


def test_pick_avoids_repeats_within_a_burst(tmp_path: Path) -> None:
    # The main fix: each pick is recorded into cat_last (recency), so within a
    # burst (one frozen instant) an already-used emoji is suppressed -- with a
    # pool at least as large as the burst, every reaction is a different glyph.
    pool = tuple(
        cats.CatEmoji(f'L{i}', chr(97 + i), 1.0, ()) for i in range(6)
    )
    brain = _brain(tmp_path, like_pool=pool)
    picks = [brain.pick_like(f'k{i}')[0].emoji_id for i in range(6)]
    assert len(set(picks)) == 6  # no repeats across the burst


def test_pick_records_recency_and_persists(tmp_path: Path) -> None:
    path = tmp_path / 'cats_state.json'
    brain = cats.CatBrain(_params(), path, random.Random(0))
    brain.clock = _ts
    chosen = brain.pick_like('k')[0].emoji_id
    assert brain.state.cat_last[chosen] == _ts()
    fresh = cats.CatBrain(_params(), path, random.Random(0))
    assert fresh.state.cat_last[chosen] == _ts()  # recency survived a restart


def test_pick_like_varies_across_keys(tmp_path: Path) -> None:
    brain = _brain(tmp_path)
    seen = {brain.pick_like(f'k{i}')[0].emoji_id for i in range(50)}
    assert len(seen) > 1  # weighted draw: not the same like for every target


def test_pick_like_empty_pool_sends_nothing(tmp_path: Path) -> None:
    brain = _brain(tmp_path, like_pool=())
    assert brain.pick_like('any') == []


# --- the sticker gate: deterministic, both conditions, per post


def test_pick_cat_returns_one_from_the_cat_pool(tmp_path: Path) -> None:
    brain = _brain(tmp_path)
    ids = {c.emoji_id for c in brain.params.pool}
    assert brain.pick_cat('chat:5001')[0].emoji_id in ids


def test_sticker_needs_both_silence_and_burst(tmp_path: Path) -> None:
    # gap=3, burst=3: the first 3 engagements build the silence; only once
    # BOTH the gap is met AND >=3 landed in the window does a sticker fire.
    brain = _brain(tmp_path, sticker_gap=3, burst_count=3)
    brain.clock = lambda: 1000.0  # all inside one burst window
    fires = [brain.should_sticker('c:1') for _ in range(5)]
    assert fires == [False, False, False, True, False]


def test_sticker_resets_the_silence_after_firing(tmp_path: Path) -> None:
    brain = _brain(tmp_path, sticker_gap=2, burst_count=1)
    brain.clock = lambda: 1000.0
    fires = [brain.should_sticker('c:1') for _ in range(6)]
    # every (gap+1)-th engagement fires, then the counter resets
    assert fires == [False, False, True, False, False, True]


def test_sticker_burst_window_expires(tmp_path: Path) -> None:
    # Spread engagements far apart: the burst never accumulates, so despite
    # plenty of silence, no sticker fires.
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
    brain = _brain(tmp_path, sticker_gap=1, burst_count=1)
    brain.clock = lambda: 1000.0
    # post A accrues silence; post B is independent (its own counter)
    assert brain.should_sticker('A') is False
    assert brain.should_sticker('B') is False
    assert brain.should_sticker('A') is True  # A's 2nd engagement fires


def test_pending_kind_round_trips(tmp_path: Path) -> None:
    path = tmp_path / 'cats_state.json'
    brain = cats.CatBrain(_params(), path, random.Random(0))
    brain.add_pending(cats.Cat(5, 900, 900, 111.0, kind='reply'))
    fresh = cats.CatBrain(_params(), path, random.Random(0))
    (restored,) = fresh.rearm(renew_all=True)
    assert restored.kind == 'reply'


def test_load_reads_the_like_pool() -> None:
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
    path = tmp_path / 'cats_state.json'
    brain = cats.CatBrain(_params(), path, random.Random(0))
    brain.state.mood = 0.5
    brain.state.catted = {'x', 'y'}
    brain.state.cat_last = {'1': 123.0}
    brain.state.next_session_at = 999.0
    brain._save()
    fresh = cats.CatBrain(_params(), path, random.Random(0))
    assert fresh.state.mood == 0.5
    assert fresh.state.catted == {'x', 'y'}
    assert fresh.state.cat_last == {'1': 123.0}
    assert fresh.state.next_session_at == 999.0


def test_corrupt_state_starts_fresh(tmp_path: Path) -> None:
    path = tmp_path / 'cats_state.json'
    path.write_text('{ not json', encoding='utf-8')
    brain = cats.CatBrain(_params(), path, random.Random(0))
    assert brain.state.mood == 0.0
    assert brain.state.catted == set()


def test_old_state_migrates_next_earliest_to_session(tmp_path: Path) -> None:
    path = tmp_path / 'cats_state.json'
    path.write_text('{"next_earliest": 555.0}', encoding='utf-8')
    brain = cats.CatBrain(_params(), path, random.Random(0))
    assert brain.state.next_session_at == 555.0  # migrated from the old key


# --- loader


def test_load_cat_params_defaults_to_disabled() -> None:
    params = cats.load_cat_params({})
    assert params.enabled is False
    assert params.watch_posts == 4


def test_load_cat_params_reads_the_pool() -> None:
    # The cat pool is the type=cat entries of the unified top-level array.
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
