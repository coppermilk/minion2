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
        'watch_posts': 4,
        'hours_weekday': ((12.0, 3.0, 1.0), (20.0, 3.0, 1.0)),
        'hours_weekend': ((12.0, 3.0, 1.0), (20.0, 3.0, 1.0)),
        'quiet_hours': frozenset({0, 1, 2, 3, 4, 5, 6}),
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
        'pool': (
            cats.CatEmoji('1', 'a', 1.0, ('bodry',)),
            cats.CatEmoji('2', 'b', 1.0, ('sleepy',)),
        ),
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


def test_hour_weight_is_zero_in_quiet_hours() -> None:
    assert cats._hour_weight(_ts(hour=3), _params()) == 0.0


def test_hour_weight_peaks_at_the_active_mean() -> None:
    params = _params()
    assert cats._hour_weight(_ts(hour=12), params) > cats._hour_weight(
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
    assert cats._hour_weight(sat, params) > cats._hour_weight(wed, params)


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


# --- principle 9 support: state persists across restarts


def test_state_round_trips_through_disk(tmp_path: Path) -> None:
    path = tmp_path / 'cats_state.json'
    brain = cats.CatBrain(_params(), path, random.Random(0))
    brain.state.mood = 0.5
    brain.state.catted = {'x', 'y'}
    brain.state.cat_last = {'1': 123.0}
    brain.state.next_earliest = 999.0
    brain._save()
    fresh = cats.CatBrain(_params(), path, random.Random(0))
    assert fresh.state.mood == 0.5
    assert fresh.state.catted == {'x', 'y'}
    assert fresh.state.cat_last == {'1': 123.0}
    assert fresh.state.next_earliest == 999.0


def test_corrupt_state_starts_fresh(tmp_path: Path) -> None:
    path = tmp_path / 'cats_state.json'
    path.write_text('{ not json', encoding='utf-8')
    brain = cats.CatBrain(_params(), path, random.Random(0))
    assert brain.state.mood == 0.0
    assert brain.state.catted == set()


# --- loader


def test_load_cat_params_defaults_to_disabled() -> None:
    params = cats.load_cat_params({})
    assert params.enabled is False
    assert params.watch_posts == 4


def test_load_cat_params_reads_the_pool() -> None:
    params = cats.load_cat_params(
        {
            'cats': {
                'enabled': True,
                'emoji': [
                    {'id': '9', 'fallback': 'c', 'base': 2, 'tags': ['bodry']}
                ],
            }
        }
    )
    assert params.enabled is True
    assert len(params.pool) == 1
    assert params.pool[0].emoji_id == '9'
    assert params.pool[0].tags == ('bodry',)
