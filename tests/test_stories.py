# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The aggregator's human-like story viewer (minions/userbot/stories.py).

Pure-logic tests: no Telethon, no network. The brain only ever plans views of
UNSEEN stories, in human-like sessions, and never re-views one it has already
watched -- the properties these tests pin down. ``main.py`` does the actual
opening/marking against Telegram; none of that is exercised here.
"""

from __future__ import annotations

import pathlib
import random
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

from minions.userbot import main
from minions.userbot.core import config
from minions.userbot.core.state import DB_NAME
from minions.userbot.core.state import Database
from minions.userbot.core.state import StateStore
from minions.userbot.engines import stories

_MANY = 50
_NOON = datetime(1970, 1, 1, 12, 0, tzinfo=UTC).timestamp()  # a waking hour
_TWO = 2
_THREE = 3
_LOG_CAP = 5
_POLL_TEST = 300.0
_POLL_LIVE = 3600.0

if TYPE_CHECKING:
    from pathlib import Path


def _params(**over: object) -> stories.StoryParams:
    base = {
        'enabled': True,
        'view_all': False,
        'include_archived': False,
        'tz_offset_hours': 0.0,
        'quiet_hours': frozenset({1, 2, 3, 4, 5, 6, 7}),
        'poll_sec': 1800.0,
        'per_session_min': 2,
        'per_session_max': 6,
        'skip_peer_prob': 0.0,
        'silent_day_prob': 0.0,
        'latency_log_mu': 1.0,
        'latency_log_sigma': 0.1,
        'gap_log_mu': 1.0,
        'gap_log_sigma': 0.1,
        'spacing_log_mu': 6.0,
        'spacing_log_sigma': 0.1,
        'dwell_min_sec': 2.0,
        'dwell_max_sec': 9.0,
        'max_peers_tracked': 500,
        'seen_per_peer': 40,
        'log_limit': 50,
        'catch_up_max': 12,
        # Push the aversion arm out of [0,1] so the Wundt peak is p=1: these
        # mechanic tests then view every unseen id (the exposure-fraction tests
        # below override this to exercise the real ~2/3 curve).
        'exposure_c1': 0.45,
        'exposure_c2': 5.0,
        'exposure_k': 8.0,
        'view_control_gain': 1.0,
    }
    base.update(over)
    return stories.StoryParams(**base)


def _store(tmp_path: Path) -> StateStore:
    """Return a state store over a temp dir (reopening reads it back)."""
    return Database(tmp_path / DB_NAME).store('stories')


def _brain(tmp_path: Path, **over: object) -> stories.StoryBrain:
    brain = stories.StoryBrain(
        _params(**over), _store(tmp_path), rng=random.Random(0)
    )
    brain.clock = lambda: _NOON
    return brain


def _seen(brain: stories.StoryBrain, peer: int) -> set[int]:
    """Return the story ids recorded seen for one peer."""
    return {
        int(row['key'].split(':')[1])
        for row in brain.store.conn.execute(
            'SELECT key FROM marks WHERE key LIKE ?', (f'{peer}:%',)
        )
    }


def _ids(n: int) -> tuple[int, ...]:
    """Return ``n`` distinct story ids -- what the acts were about."""
    return tuple(range(900, 900 + n))


def _cand(
    peer: int, ids: tuple[int, ...], *, last_ts: float = 0.0
) -> stories.StoryCandidate:
    return stories.StoryCandidate(
        peer_id=peer,
        story_ids=ids,
        max_id=max(ids),
        last_ts=last_ts,
    )


# --- unseen / never re-view


def test_unseen_subtracts_the_seen_set(tmp_path: Path) -> None:
    """Check unseen subtracts the seen set."""
    brain = _brain(tmp_path)
    cand = _cand(7, (1, 2, 3))
    assert brain.unseen(cand) == (1, 2, 3)
    brain.mark_viewed(7, (1, 2), ts=_NOON)
    assert brain.unseen(cand) == (3,)


def test_a_fully_seen_peer_is_not_eligible(tmp_path: Path) -> None:
    """Check a fully seen peer is not eligible."""
    brain = _brain(tmp_path)
    brain.mark_viewed(7, (1, 2, 3), ts=_NOON)
    assert brain.plan([_cand(7, (1, 2, 3))], now=_NOON) == []


def test_plan_only_views_unseen_ids(tmp_path: Path) -> None:
    """Check plan only views unseen ids."""
    brain = _brain(tmp_path)
    brain.mark_viewed(7, (1,), ts=_NOON)
    views = brain.plan([_cand(7, (1, 2, 3))], now=_NOON)
    assert len(views) == 1
    assert views[0].story_ids == (2, 3)
    assert views[0].max_id == _THREE


# --- gates


def test_disabled_plans_nothing(tmp_path: Path) -> None:
    """Check disabled plans nothing."""
    brain = _brain(tmp_path, enabled=False)
    assert brain.plan([_cand(7, (1,))], now=_NOON) == []


def test_quiet_hours_plan_nothing(tmp_path: Path) -> None:
    """Check quiet hours plan nothing."""
    brain = _brain(tmp_path, quiet_hours=frozenset({12}))
    assert brain.plan([_cand(7, (1,))], now=_NOON) == []


def test_silent_day_plans_nothing(tmp_path: Path) -> None:
    """Check silent day plans nothing."""
    # prob 1.0 makes every day silent, deterministically.
    brain = _brain(tmp_path, silent_day_prob=1.0)
    assert brain.plan([_cand(7, (1,))], now=_NOON) == []


def test_between_session_cooldown_blocks(tmp_path: Path) -> None:
    """Check between session cooldown blocks."""
    brain = _brain(tmp_path)
    brain.state.next_session_at = _NOON + 10_000.0
    assert brain.plan([_cand(7, (1,))], now=_NOON) == []


def test_blocked_reason_explains_an_empty_plan(tmp_path: Path) -> None:
    """Check blocked reason explains an empty plan."""
    assert _brain(tmp_path).blocked_reason(_NOON) is None  # open
    off = _brain(tmp_path, enabled=False)
    assert off.blocked_reason(_NOON) == 'disabled'
    quiet = _brain(tmp_path, quiet_hours=frozenset({12}))
    assert quiet.blocked_reason(_NOON) == 'quiet-hours'
    cool = _brain(tmp_path)
    cool.state.next_session_at = _NOON + 100.0
    assert cool.blocked_reason(_NOON) == 'cooldown 100s'


# --- session shape


def test_session_is_capped_and_staggered(tmp_path: Path) -> None:
    """Check session is capped and staggered."""
    brain = _brain(tmp_path, per_session_min=3, per_session_max=3)
    cands = [_cand(p, (1,), last_ts=float(p)) for p in range(10)]
    views = brain.plan(cands, now=_NOON)
    assert len(views) == _THREE  # capped, not all ten
    whens = [v.when for v in views]
    assert whens == sorted(whens)  # staggered forward in time
    assert whens[0] > _NOON  # a beat after "opening the app"


def test_freshest_first(tmp_path: Path) -> None:
    """Check freshest first."""
    brain = _brain(tmp_path, per_session_min=1, per_session_max=1)
    older = _cand(1, (1,), last_ts=100.0)
    newer = _cand(2, (1,), last_ts=999.0)
    views = brain.plan([older, newer], now=_NOON)
    assert [v.peer_id for v in views] == [2]  # the newer story leads


def test_next_session_is_pushed_forward(tmp_path: Path) -> None:
    """Check next session is pushed forward."""
    brain = _brain(tmp_path)
    brain.plan([_cand(7, (1,))], now=_NOON)
    assert brain.state.next_session_at > _NOON


def test_skipping_still_views_at_least_one(tmp_path: Path) -> None:
    """Check skipping still views at least one."""
    # Always-skip would empty the glance; the brain still opens the freshest.
    brain = _brain(tmp_path, skip_peer_prob=1.0)
    views = brain.plan([_cand(7, (1,), last_ts=5.0)], now=_NOON)
    assert [v.peer_id for v in views] == [7]


def test_view_all_sweeps_without_the_per_session_cap(tmp_path: Path) -> None:
    """Catch-up ignores the per-session cap/skip (up to the catch-up cap)."""
    # Ten peers is under catch_up_max (12), so all are swept: no cap, no skip.
    brain = _brain(
        tmp_path, view_all=True, per_session_max=2, skip_peer_prob=1.0
    )
    cands = [_cand(p, (1,), last_ts=float(p)) for p in range(10)]
    views = brain.plan(cands, now=_NOON)
    assert len(views) == len(cands)  # all ten, no per-session cap, no skip


def test_view_all_respects_the_cooldown(tmp_path: Path) -> None:
    """Catch-up now waits between sessions, so a backlog drains over polls."""
    brain = _brain(tmp_path, view_all=True)
    brain.plan([_cand(p, (1,), last_ts=float(p)) for p in range(5)], now=_NOON)
    # After a catch-up session the next poll is on the normal cooldown, so the
    # night's backlog is cleared a session at a time -- not swept every poll.
    assert brain.blocked_reason(_NOON + 1) is not None


def test_view_all_caps_at_catch_up_max(tmp_path: Path) -> None:
    """A backlog larger than catch_up_max is viewed a capped chunk per poll."""
    brain = _brain(tmp_path, view_all=True, catch_up_max=_LOG_CAP)
    cands = [_cand(p, (1,), last_ts=float(p)) for p in range(_MANY)]
    views = brain.plan(cands, now=_NOON)
    assert len(views) == _LOG_CAP  # the freshest catch_up_max, rest next poll


# --- marking / bookkeeping


def test_mark_viewed_dedups_and_counts_fresh(tmp_path: Path) -> None:
    """Check mark viewed dedups and counts fresh."""
    brain = _brain(tmp_path)
    brain.mark_viewed(7, (1, 2), ts=_NOON)
    brain.mark_viewed(7, (2, 3), ts=_NOON)  # 2 already seen -> only 3 is fresh
    assert brain.seen_count() == _THREE
    assert _seen(brain, 7) == {1, 2, 3}


# --- Berlyne exposure control (view a fraction toward the Wundt peak)


_EXPO_TOL = 0.05
_EXPO_POLLS = 300
_R_TARGET = 0.20
_FIVE = 5


def test_view_split_converges_to_the_wundt_peak(tmp_path: Path) -> None:
    """viewed/offered steers to ~0.675, not 1 -- we skip ~1/3 on purpose."""
    brain = _brain(tmp_path, exposure_c2=0.90)  # real curve (not view-all)
    p_star = brain._view_target()
    assert abs(p_star - 0.675) < _EXPO_TOL
    peer, sid = 7, 0
    for _ in range(_EXPO_POLLS):
        unseen = (sid, sid + 1, sid + 2)
        sid += 3
        view_ids, skip_ids = brain._view_split(peer, unseen, p_star)
        brain._record_skips(peer, skip_ids)  # skips: offered, never viewed
        brain.mark_viewed(peer, view_ids, ts=_NOON)  # views: offered + viewed
    key = str(peer)
    ratio = brain.ledger.row(key).taken / brain.ledger.row(key).offered
    assert abs(ratio - p_star) < _EXPO_TOL


def test_skipped_stories_are_recorded_seen(tmp_path: Path) -> None:
    """A deliberately-skipped story is not re-offered (marked seen at once)."""
    brain = _brain(tmp_path, exposure_c2=0.90)
    _view, skip = brain._view_split(7, (1, 2, 3, 4, 5), brain._view_target())
    brain._record_skips(7, skip)
    for sid in skip:
        assert sid in _seen(brain, 7)  # would not be offered again


def test_view_all_exposure_still_views_every_story(tmp_path: Path) -> None:
    """With the aversion arm pushed out, the peak is p=1 -- view everything."""
    brain = _brain(tmp_path)  # base _params sets exposure_c2=5.0
    view_ids, skip_ids = brain._view_split(7, (1, 2, 3), brain._view_target())
    assert view_ids == (1, 2, 3)
    assert skip_ids == ()


# --- reciprocity (heart/thumb a fraction of the stories we view)


def test_react_fraction_converges_to_target(tmp_path: Path) -> None:
    """recip/taken steers to ~0.20 -- an occasional heart, not silence.

    Closed-loop: each poll plans reactions, then commits the views and hearts
    to the ledger (as the glue does), so the running fraction feeds back.
    """
    brain = _brain(tmp_path)  # react_fraction_target defaults to 0.20
    peer = 7
    budget = 1_000_000  # effectively uncapped for the ratio test
    sid = 1
    for _ in range(_EXPO_POLLS):
        ids = (sid, sid + 1, sid + 2)
        sid += 3
        r_ids, budget = brain._plan_reacts(peer, ids, budget)
        brain.mark_viewed(peer, ids, ts=_NOON)
        brain.mark_reacted(peer, tuple(r_ids), _NOON)
    key = str(peer)
    ratio = brain.ledger.row(key).recip / brain.ledger.row(key).taken
    assert abs(ratio - _R_TARGET) < _EXPO_TOL


def test_react_budget_caps_the_day(tmp_path: Path) -> None:
    """No more than react_max_per_day reactions go out, then it floors at 0."""
    brain = _brain(tmp_path, react_max_per_day=_THREE)
    assert brain._react_budget(_NOON) == _THREE
    brain.mark_reacted(7, _ids(_TWO), _NOON)
    assert brain._react_budget(_NOON) == _THREE - _TWO
    brain.mark_reacted(7, _ids(_FIVE), _NOON)  # overshoots via one call
    assert brain._react_budget(_NOON) == 0


def test_react_disabled_plans_no_reactions(tmp_path: Path) -> None:
    """With reactions off the budget is zero and nothing is planned."""
    brain = _brain(tmp_path, react_enabled=False)
    budget = brain._react_budget(_NOON)
    assert budget == 0
    r_ids, _budget = brain._plan_reacts(7, (1, 2, 3), budget)
    assert r_ids == ()


def test_react_counter_rolls_over_daily(tmp_path: Path) -> None:
    """The daily counter resets at local midnight; the per-peer tally stays."""
    brain = _brain(tmp_path)
    brain.mark_reacted(7, _ids(_FIVE), _NOON)
    assert brain.ledger.recip_today == _FIVE
    brain.mark_reacted(7, _ids(_TWO), _NOON + 86400.0)  # the next day
    assert brain.ledger.recip_today == _TWO  # reset, then +2
    assert brain.ledger.row('7').recip == _FIVE + _TWO  # cumulative per peer


def test_plan_attaches_reactions(tmp_path: Path) -> None:
    """A planned view carries its react ids (subset) and a glyph from pool."""
    brain = _brain(tmp_path, react_fraction_target=1.0)
    views = brain.plan([_cand(7, (1, 2, 3))], now=_NOON)
    assert views
    view = views[0]
    assert view.react_ids == view.story_ids  # target 1.0 -> react to all seen
    assert view.react_emoji in brain.params.react_pool


def test_warmth_lists_recent_peers_first(tmp_path: Path) -> None:
    """warmth() reports per-peer p/r/index, most RECENT peer first."""
    brain = _brain(tmp_path)
    # peer 7 (earlier): viewed 2 of 3 offered (p~0.67), reacted to 1 (r 0.5)
    brain._record_skips(7, (100,))  # 1 offered, skipped
    brain.mark_viewed(7, (101, 102), ts=_NOON)
    brain.mark_reacted(7, _ids(1), _NOON)
    # peer 8 (an hour later): viewed all, no reactions (r 0)
    brain.mark_viewed(8, (200, 201), ts=_NOON + 3600)
    rows = brain.warmth()
    assert [w.peer_id for w in rows] == [8, 7]  # newest first
    cool, warm = rows
    assert warm.r == 0.5  # noqa: PLR2004 -- 1 reaction of 2 viewed
    assert cool.r == 0.0  # no reactions -> zero reciprocity
    assert cool.index < warm.index  # listed first by recency, not by score


def test_views_today_counts_only_todays_views(tmp_path: Path) -> None:
    """/status shows today's views: the log entries on today's local date."""
    brain = _brain(tmp_path)
    brain.mark_viewed(1, (1, 2), ts=_NOON - 86400.0)  # yesterday: excluded
    brain.mark_viewed(2, (3,), ts=_NOON)  # today: 1
    brain.mark_viewed(3, (4, 5), ts=_NOON)  # today: 2
    assert brain.views_today(_NOON, 0.0) == _THREE  # today's 1 + 2 only


def test_mark_viewed_is_idempotent(tmp_path: Path) -> None:
    """Check mark viewed is idempotent."""
    brain = _brain(tmp_path)
    brain.mark_viewed(7, (1, 2), ts=_NOON)
    before = brain.seen_count()
    brain.mark_viewed(7, (1, 2), ts=_NOON)  # nothing new
    assert brain.seen_count() == before
    assert len(brain.state.log) == 1  # no second log line


def test_seen_list_is_bounded_per_peer(tmp_path: Path) -> None:
    """Check seen list is bounded per peer."""
    brain = _brain(tmp_path, seen_per_peer=3)
    brain.mark_viewed(7, tuple(range(10)), ts=_NOON)
    assert _seen(brain, 7) == {7, 8, 9}


def test_tracked_peers_are_lru_bounded(tmp_path: Path) -> None:
    """Check tracked peers are lru bounded."""
    brain = _brain(tmp_path, max_peers_tracked=2)
    for peer in (1, 2, 3):  # a minute apart, so "least recent" is a fact
        brain.mark_viewed(peer, (1,), ts=_NOON + peer * 60)
    assert {r.peer_id for r in brain.store.peers()} == {
        2,
        3,
    }  # peer 1 evicted


def test_every_view_is_logged(tmp_path: Path) -> None:
    """Check every view is logged."""
    brain = _brain(tmp_path)
    brain.mark_viewed(1, (1, 2), ts=_NOON)
    brain.mark_viewed(2, (9,), ts=_NOON)
    log = brain.recent_log(10)
    assert len(log) == _TWO  # one entry per view
    by_peer = {e.peer_id: e for e in log}
    assert by_peer[1].count == _TWO
    assert by_peer[2].count == 1


def test_recent_log_is_newest_first(tmp_path: Path) -> None:
    """Check recent log is newest first."""
    brain = _brain(tmp_path)
    brain.mark_viewed(1, (1,), ts=_NOON)
    brain.mark_viewed(2, (1,), ts=_NOON)
    recent = brain.recent_log(5)
    assert [r.peer_id for r in recent] == [2, 1]


def test_log_is_bounded(tmp_path: Path) -> None:
    """Check log is bounded."""
    brain = _brain(tmp_path, log_limit=_LOG_CAP)
    for peer in range(_MANY):
        brain.mark_viewed(peer, (1,), ts=_NOON)
    assert len(brain.state.log) == _LOG_CAP


def test_state_survives_reopening_the_store(tmp_path: Path) -> None:
    """Seen marks and the odometer both survive a reopen."""
    store = _store(tmp_path)
    first = stories.StoryBrain(_params(), store, rng=random.Random(0))
    first.mark_viewed(7, (1, 2, 3), ts=_NOON)
    reopened = stories.StoryBrain(_params(), store, rng=random.Random(0))
    assert reopened.unseen(_cand(7, (1, 2, 3, 4))) == (4,)
    assert reopened.seen_count() == _THREE


def test_load_story_params_mode_selects_poll(tmp_path: Path) -> None:
    """Check load story params mode selects poll."""
    data = {
        'engines': {
            'stories': {
                'enabled': True,
                'poll_sec_test': 300,
                'poll_sec_live': 3600,
            }
        }
    }
    assert stories.load_story_params(data, 'test').poll_sec == _POLL_TEST
    assert stories.load_story_params(data, 'live').poll_sec == _POLL_LIVE


def test_include_archived_defaults_off(tmp_path: Path) -> None:
    """Check include archived defaults off."""
    blank = {'engines': {'stories': {}}}
    assert not stories.load_story_params(blank).include_archived
    on = stories.load_story_params(
        {'engines': {'stories': {'include_archived': True}}}
    )
    assert on.include_archived


# --- the glance: who we are opening, and who we are not


def _verdicts(brain: stories.StoryBrain) -> dict[int, str]:
    """Return the last glance's verdict per peer."""
    return {row.peer_id: row.verdict for row in brain.last_glance.peers}


def test_every_peer_with_stories_gets_exactly_one_verdict(
    tmp_path: Path,
) -> None:
    """The glance accounts for everyone the feed showed, once each.

    This is the whole point of the readout: an operator looking at
    Telegram sees these people, and /status has to say something about
    each of them rather than only about the ones we chose.
    """
    brain = _brain(tmp_path, per_session_max=1, skip_peer_prob=0.9)
    brain.mark_viewed(3, (30, 31), ts=_NOON)  # 3 has nothing new
    cands = [
        _cand(1, (10, 11), last_ts=_NOON),
        _cand(2, (20,), last_ts=_NOON - 1),
        _cand(3, (30, 31), last_ts=_NOON - 2),
    ]
    views = brain.plan(cands, now=_NOON)

    glance = brain.last_glance
    assert glance.at == _NOON
    assert [row.peer_id for row in glance.peers] == [1, 2, 3]
    assert _verdicts(brain)[3] == stories.NOTHING_NEW
    # Whoever is queued is 'viewing' and reports how many we will open;
    # everyone else with unseen stories was passed.
    opened = {v.peer_id: len(v.story_ids) for v in views}
    for row in glance.peers:
        if row.peer_id in opened:
            assert row.verdict == stories.VIEWING
            assert row.viewing == opened[row.peer_id]
        else:
            assert row.viewing == 0
            assert row.verdict in {stories.PASSED, stories.NOTHING_NEW}


def test_a_blocked_session_says_why_for_everyone(tmp_path: Path) -> None:
    """Quiet hours name themselves on every peer, not just on the header."""
    brain = _brain(tmp_path, quiet_hours=frozenset({12}))
    assert brain.plan([_cand(1, (10,))], now=_NOON) == []
    glance = brain.last_glance
    assert glance.blocked
    assert _verdicts(brain) == {1: glance.blocked}
    assert all(row.viewing == 0 for row in glance.peers)


def test_the_archived_feed_is_marked_as_such(tmp_path: Path) -> None:
    """A peer from the hidden feed is flagged, so the list matches Telegram."""
    brain = _brain(tmp_path)
    archived = replace(_cand(2, (20,)), hidden=True)
    brain.plan([_cand(1, (10,)), archived], now=_NOON)
    flags = {row.peer_id: row.hidden for row in brain.last_glance.peers}
    assert flags == {1: False, 2: True}


def test_the_glance_counts_what_is_up_and_what_is_new(tmp_path: Path) -> None:
    """Active is what they posted; unseen is what we have not opened."""
    brain = _brain(tmp_path)
    brain.mark_viewed(1, (10,), ts=_NOON)
    brain.plan([_cand(1, (10, 11, 12))], now=_NOON)
    row = brain.last_glance.peers[0]
    assert (row.active, row.unseen) == (_THREE, _TWO)


def test_an_empty_feed_leaves_an_empty_glance(tmp_path: Path) -> None:
    """Nobody has stories: the readout says so rather than going stale."""
    brain = _brain(tmp_path)
    brain.plan([], now=_NOON)
    assert brain.last_glance.peers == ()
    assert brain.last_glance.at == _NOON


# --- the Telethon side: what Telegram refuses is not recorded -------------
# The brain above is pure; these drive the glue, because the bug they pin is
# in the seam. A story can expire between the plan and the send, and the
# adapter reports that by RETURNING False rather than raising -- so the
# except-guard the module relied on never fired and everything got marked
# seen regardless.

_STORY_A = 101
_STORY_B = 102


class _FakeAccount:
    """Stand in for the one door to Telegram, refusing what it is told to."""

    def __init__(self, refuse: set[int] | None = None) -> None:
        self.refuse = refuse or set()
        self.viewed: list[int] = []
        self.reacted: list[int] = []
        self.read_up_to = 0

    async def input_peer(self, peer_id: int) -> object:
        return peer_id

    async def view_story(self, _peer: object, story_id: int) -> bool:
        if story_id in self.refuse:
            return False  # expired between the plan and the send
        self.viewed.append(story_id)
        return True

    async def react_to_story(
        self, _peer: object, story_id: int, _emoji: str
    ) -> bool:
        self.reacted.append(story_id)
        return True

    async def read_stories(self, _peer: object, max_id: int) -> bool:
        self.read_up_to = max_id
        return True


def _watcher(
    tmp_path: Path, account: _FakeAccount
) -> tuple[object, stories.StoryBrain, _Names]:
    """Build a story watcher over a real brain and a fake door."""
    from minions.userbot.glue import stories as glue

    brain = _brain(tmp_path, dwell_min_sec=0.0, dwell_max_sec=0.0)
    names = _Names()
    watch = glue.StoryWatch(
        glue.StoryDeps(
            account=account,  # type: ignore[arg-type]
            brain=brain,
            source=0,
            learn=names,
            name=lambda peer_id: f'@peer{peer_id}',
        )
    )
    return watch, brain, names


class _Names:
    """The host's peer resolver, counting what it was asked.

    Resolving a peer is a Telegram round trip, so a view that recorded
    nothing must not spend one.
    """

    def __init__(self) -> None:
        self.asked = 0

    async def __call__(self, peer_id: int) -> str:
        self.asked += 1
        return f'@peer{peer_id}'


def _view(**over: object) -> stories.StoryView:
    base: dict[str, object] = {
        'peer_id': 7,
        'story_ids': (_STORY_A, _STORY_B),
        'max_id': _STORY_B,
        'when': _NOON,
    }
    base.update(over)
    return stories.StoryView(**base)  # type: ignore[arg-type]


def test_a_story_that_expired_is_left_unseen(tmp_path: Path) -> None:
    """A refused story is not recorded viewed, so the next poll may retry.

    Marking it seen would record a view that never happened: the peer's
    exposure climbs on it and the id is burnt for good.
    """
    import asyncio

    account = _FakeAccount(refuse={_STORY_A})
    watch, brain, _ = _watcher(tmp_path, account)

    asyncio.run(watch._view_later(_view()))

    assert account.viewed == [_STORY_B]
    assert _seen(brain, 7) == {_STORY_B}  # the refused one stays open
    assert brain.state.total_views == 1


def test_a_story_refused_gets_no_reaction(tmp_path: Path) -> None:
    """No reaction is placed on a story we were not allowed to open."""
    import asyncio

    account = _FakeAccount(refuse={_STORY_A})
    watch, _, _ = _watcher(tmp_path, account)

    asyncio.run(watch._view_later(_view(react_ids=(_STORY_A, _STORY_B))))

    assert account.reacted == [_STORY_B]
    assert watch.unplaced == 1  # one planned reaction did not go out


def test_a_wholly_refused_view_records_nothing(tmp_path: Path) -> None:
    """When Telegram opens none of them, the peer keeps every story unseen."""
    import asyncio

    account = _FakeAccount(refuse={_STORY_A, _STORY_B})
    watch, brain, _ = _watcher(tmp_path, account)

    watch, brain, names = _watcher(tmp_path, account)

    asyncio.run(watch._view_later(_view()))

    assert account.viewed == []
    assert _seen(brain, 7) == set()
    assert brain.state.total_views == 0
    assert names.asked == 0  # no round trip for a view that recorded nothing


def test_a_story_ends_up_ignored_seen_or_liked(tmp_path: Path) -> None:
    """The three outcomes a story can have, each one row in the history.

    A skipped story is an ``ignore``, an opened one a ``seen``, and a heart
    is a ``like`` ON TOP of the seen -- a second row, because it is a second
    moment: we open it, and then, a beat later, react. What the counters say
    has to be exactly what these rows add up to.
    """
    brain = _brain(tmp_path, exposure_c2=0.9)  # the real ~2/3 curve
    peer, offered = 42, tuple(range(600, 610))
    view, skip = brain._view_split(peer, offered, brain._view_target())
    brain._record_skips(peer, skip)
    brain.mark_viewed(peer, view, _NOON)
    brain.mark_reacted(peer, view[:1], _NOON + 60)

    assert brain.store.acts(peer) == {
        'ignore': len(skip),
        'seen': len(view),
        'like': 1,
    }
    assert brain.store.tally(peer) == {
        'offered': len(offered),
        'taken': len(view),
        'recip': 1,
    }


def test_the_counters_stay_a_running_total_of_the_log(tmp_path: Path) -> None:
    """Drive the real engine for a week; the invariant holds per person.

    The counters are what every percentage in /status is computed from, and
    before the log they were the only copy of their own input -- so a
    surprising number could not be checked, only re-simulated.
    """
    brain = _brain(tmp_path, exposure_c2=0.9, react_fraction_target=0.1)
    sid = 1
    for day in range(7):
        for peer in (11, 22, 33):
            fresh = tuple(range(sid, sid + 2))
            sid += 2
            at = _NOON + day * 86400.0
            view, skip = brain._view_split(peer, fresh, brain._view_target())
            brain._record_skips(peer, skip)
            brain.mark_viewed(peer, view, at)
            react, _ = brain._plan_reacts(peer, view, brain._react_budget(at))
            brain.mark_reacted(peer, react, at)

    for row in brain.store.peers():
        logged = brain.store.tally(row.peer_id)
        assert row.offered == logged['offered']
        assert row.taken == logged['taken']
        assert row.recip == logged['recip']


_CIRCLE = (11, 22, 33)
_WEEK_LIKES_MIN = 1.0
_WEEK_LIKES_MAX = 2.8
_SEEDS = 30


def _shipped() -> stories.StoryParams:
    """Load the story params the bot actually ships with."""
    root = pathlib.Path(main.__file__).with_name('aggregator_constants.json')
    data = config.read_json(root)
    config.apply_persona(data)
    return stories.load_story_params(data)


def _likes_in_a_week(tmp_path: Path, seed: int) -> int:
    """Run the shipped engine over a week of a small circle; count hearts."""
    db = Database(tmp_path / f'{seed}.db')
    brain = stories.StoryBrain(_shipped(), db.store('stories'))
    brain.rng = random.Random(seed)
    sid, liked = 1, 0
    for day in range(7):
        for peer in _CIRCLE:
            fresh, sid = (sid,), sid + 1
            at = _NOON + day * 86400.0
            view, skip = brain._view_split(peer, fresh, brain._view_target())
            brain._record_skips(peer, skip)
            brain.mark_viewed(peer, view, at)
            react, _ = brain._plan_reacts(peer, view, brain._react_budget(at))
            brain.mark_reacted(peer, react, at)
            liked += len(react)
    return liked


def test_the_shipped_rate_leaves_one_or_two_likes_a_week(
    tmp_path: Path,
) -> None:
    """A heart has to stay rare, or it stops meaning anything.

    We watch a lot and react seldom: the reaction is the only thing the
    other person is meant to read as deliberate, and one arriving every few
    days says that, where one every other view says only that something is
    running. Pinned as a WEEKLY COUNT over the real config rather than as
    the fraction, because the fraction is the knob and this is the effect
    -- and a circle of three posting once a day is the case it was set for.
    At the previous 0.20 the same week averages 3.7, well outside this.
    """
    got = [_likes_in_a_week(tmp_path, seed) for seed in range(_SEEDS)]

    assert _WEEK_LIKES_MIN <= sum(got) / len(got) <= _WEEK_LIKES_MAX
