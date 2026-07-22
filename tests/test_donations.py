"""donations bot: feed parsing, Russian render, and clean idle.

Hermetic: no network, no tokens. Streamlabs parsing is exercised on a
fixed payload; the poll loop on feed/sender doubles.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from minion_core.adapters.donations import BED_TTL_SEC
from minion_core.adapters.donations import AlertSpec
from minion_core.adapters.donations import BedRoster
from minion_core.adapters.donations import DeadFeed
from minion_core.adapters.donations import Donation
from minion_core.adapters.donations import DonationAlerts
from minion_core.adapters.donations import RevolutFeed
from minion_core.adapters.donations import StreamlabsFeed
from minion_core.adapters.donations import _parse
from minion_core.adapters.donations import _parse_revolut
from minion_core.adapters.donations import bed_roster
from minion_core.adapters.donations import feed_for
from minion_core.adapters.donations import feeds_for
from minions.bots.donations.main import _BedCommand
from minions.bots.donations.main import build
from minions.bots.donations.main import load_messages
from minions.bots.donations.main import main
from minions.bots.donations.main import render
from minions.bots.donations.main import render_bed
from tests.conftest import make_cfg
from tests.conftest import make_env

if TYPE_CHECKING:
    from pathlib import Path


def test_streamlabs_parse_orders_filters_and_drops_bad() -> None:
    """Only ids past the cursor survive; malformed rows are dropped."""
    payload = {
        'data': [
            {
                'donation_id': '7',
                'name': 'Bob',
                'amount': '5',
                'currency': 'USD',
                'message': 'why is the sky blue?',
            },
            {
                'donation_id': '5',
                'name': 'Ann',
                'amount': '2.5',
                'currency': 'EUR',
                'message': '',
            },
            {'donation_id': 'nope', 'name': 'ghost'},
        ],
    }
    alerts = _parse(payload, 5, 'Streamlabs')
    assert [a.ident for a in alerts] == [7]  # 5 == cursor, 'nope' bad
    assert alerts[0].name == 'Bob'
    assert alerts[0].amount == '5'  # bare number
    assert alerts[0].currency == 'USD'  # kept apart for the symbol
    assert alerts[0].platform == 'Streamlabs'
    assert alerts[0].message == 'why is the sky blue?'


def test_streamlabs_parse_tolerates_junk_payloads() -> None:
    """A non-dict payload or missing data is empty, never a crash."""
    assert _parse([], 0, 'Streamlabs') == []
    assert _parse({'data': 'oops'}, 0, 'Streamlabs') == []
    assert _parse({'data': [42, {'x': 1}]}, 0, 'Streamlabs') == []


def test_render_carries_who_how_much_and_the_question() -> None:
    """The alert names the donor, the amount+symbol and the question."""
    templates = load_messages()
    alert = Donation(1, 'Streamlabs', 'Bob', '500', 'RUB', 'why sky blue?')
    text = render(templates, alert)
    assert 'Bob' in text  # the donor, also drawn in the bed
    assert text.count('Bob') == 2  # the line AND the bed
    assert '500' in text
    assert templates['cur_RUB'] in text  # RUB -> the ruble sign
    assert '500' + templates['cur_RUB'] in text  # amount+symbol, no gap
    assert 'why sky blue?' in text
    assert '<i>why sky blue?</i>' in text  # the message is italicised
    assert '<pre>' in text  # the bed rides a monospace block
    assert 'Streamlabs' in text
    assert not text.isascii()  # the Russian template is exercised


def test_render_uses_dollar_for_usd() -> None:
    """The symbol follows the currency code, not a hardcoded ruble."""
    templates = load_messages()
    text = render(templates, Donation(1, 'S', 'Ann', '5', 'USD', 'hi'))
    assert '5' + templates['cur_USD'] in text  # 5$


def test_render_falls_back_for_anon_and_empty_message() -> None:
    """No name -> anonymous; no message -> the no-question filler."""
    templates = load_messages()
    text = render(templates, Donation(1, 'Streamlabs', '', '10', 'USD', ''))
    assert templates['anonymous'] in text
    assert templates['no_message'] in text


def test_render_escapes_untrusted_name_and_message() -> None:
    """A donor name or message with HTML is escaped, never injected."""
    templates = load_messages()
    text = render(templates, Donation(1, 'S', '<b>x', '1', 'USD', '<i>hi'))
    assert '<b>x' not in text  # the raw tag never survives
    assert '&lt;b&gt;x' in text  # escaped instead
    assert '&lt;i&gt;hi' in text  # the message too


def test_feed_for_selects_platforms_and_degrades() -> None:
    """Streamlabs and Revolut are wired; an unknown name idles."""
    labs = feed_for('streamlabs', {'STREAMLABS_TOKEN': 'x'})
    assert isinstance(labs, StreamlabsFeed)
    assert labs.live
    assert labs.name == 'Streamlabs'
    rev = feed_for('revolut', {'REVOLUT_TOKEN': 'y'})
    assert isinstance(rev, RevolutFeed)
    assert rev.live
    assert rev.name == 'Revolut'
    unknown = feed_for('paypal', {})
    assert isinstance(unknown, DeadFeed)
    assert not unknown.live  # not wired -> idle
    assert not feed_for('streamlabs', {}).live  # tokenless -> idle
    assert not feed_for('revolut', {}).live  # tokenless -> idle


def test_feeds_for_runs_several_platforms_at_once() -> None:
    """A comma list builds one feed per name, order-independent."""
    feeds = feeds_for(
        ' revolut , streamlabs ',
        {'STREAMLABS_TOKEN': 'a', 'REVOLUT_TOKEN': 'b'},
    )
    assert [f.name for f in feeds] == ['Revolut', 'Streamlabs']
    assert all(f.live for f in feeds)
    assert feeds_for('', {}) == []  # nothing configured -> no feeds


def test_revolut_parse_keeps_completed_incoming_only() -> None:
    """Only completed credit legs past the cursor become alerts."""
    payload = [
        {
            'state': 'completed',
            'completed_at': '2026-07-21T10:00:01.500Z',
            'reference': 'thanks for the stream!',
            'legs': [
                {
                    'amount': 10.0,
                    'currency': 'EUR',
                    'counterparty': {'name': 'John D'},
                }
            ],
        },
        {  # outgoing (negative leg) -> dropped
            'state': 'completed',
            'completed_at': '2026-07-21T11:00:00Z',
            'legs': [{'amount': -5.0, 'currency': 'EUR'}],
        },
        {  # not completed -> dropped
            'state': 'pending',
            'completed_at': '2026-07-21T12:00:00Z',
            'legs': [{'amount': 3.0, 'currency': 'EUR'}],
        },
    ]
    alerts = _parse_revolut(payload, 0, 'Revolut')
    assert len(alerts) == 1
    assert alerts[0].name == 'John D'
    assert alerts[0].amount == '10'  # whole number, no trailing .0
    assert alerts[0].currency == 'EUR'
    assert alerts[0].message == 'thanks for the stream!'
    assert alerts[0].platform == 'Revolut'
    assert alerts[0].ident > 0  # a millisecond timestamp cursor


class _FeedDouble:
    """A feed that yields fixed alerts newer than the cursor."""

    name = 'Test'

    def __init__(self, alerts: list[Donation]) -> None:
        self._alerts = alerts

    @property
    def live(self) -> bool:
        return True

    def fetch_after(self, cursor: int, /) -> list[Donation]:
        return [a for a in self._alerts if a.ident > cursor]


class _SenderDouble:
    """A sender that records what it posted and where."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    @property
    def live(self) -> bool:
        return True

    def send(self, chat: str, text: str) -> None:
        self.sent.append((chat, text))


def test_alerts_posts_new_advances_and_never_duplicates(
    tmp_path: Path,
) -> None:
    """Each new alert posts once; the high-water mark persists."""
    cfg = make_cfg(tmp_path / 'drive')
    feed = _FeedDouble(
        [
            Donation(1, 'Test', 'A', '1', 'USD', 'q1'),
            Donation(2, 'Test', 'B', '2', 'USD', 'q2'),
        ]
    )
    sender = _SenderDouble()
    spec = AlertSpec(
        chat='@chan',
        state=cfg.state,
        render=lambda a: f'{a.name}:{a.amount}',
    )
    src = DonationAlerts([feed], sender, spec)

    src.drain_once()
    assert sender.sent == [('@chan', 'A:1'), ('@chan', 'B:2')]
    src.drain_once()  # the per-feed high-water persisted
    assert len(sender.sent) == 2  # no duplicate posts


def test_alerts_drains_each_feed_on_its_own_cursor(tmp_path: Path) -> None:
    """Two platforms post independently, each with its own offset file.

    Streamlabs ids are small; Revolut idents are millisecond timestamps.
    A shared cursor would let the huge Revolut cursor shadow Streamlabs,
    so each feed must keep a separate high-water file.
    """
    cfg = make_cfg(tmp_path / 'drive')
    labs = _FeedDouble([Donation(5, 'Streamlabs', 'A', '1', 'USD', '')])
    rev = _FeedDouble(
        [Donation(1_700_000_000_000, 'Revolut', 'B', '2', 'EUR', '')]
    )
    labs.name = 'Streamlabs'
    rev.name = 'Revolut'
    sender = _SenderDouble()
    spec = AlertSpec(chat='@c', state=cfg.state, render=lambda a: a.platform)
    src = DonationAlerts([labs, rev], sender, spec)

    src.drain_once()
    assert sorted(text for _, text in sender.sent) == ['Revolut', 'Streamlabs']
    assert (cfg.state / 'donations-streamlabs.offset').exists()
    assert (cfg.state / 'donations-revolut.offset').exists()
    src.drain_once()  # both cursors persisted independently
    assert len(sender.sent) == 2  # no re-posts


def test_build_and_main_idle_without_config(tmp_path: Path) -> None:
    """REQ-DEG-001: unconfigured, the graph drains clean and exits 0."""
    cfg = make_cfg(tmp_path / 'drive')
    graph = build(cfg, {})
    assert list(graph(iter(()))) == []  # tokenless: drains clean
    assert main(make_env(tmp_path / 'drive')) == 0


def test_render_links_each_platform_to_its_own_tip_page() -> None:
    """A Streamlabs gift links to Streamlabs; a Revolut one to Revolut."""
    templates = load_messages()
    labs = render(templates, Donation(1, 'Streamlabs', 'A', '5', 'USD', 'q'))
    assert templates['link_streamlabs'] in labs
    rev = render(templates, Donation(2, 'Revolut', 'B', '5', 'EUR', 'q'))
    assert templates['link_revolut'] in rev
    assert 'revolut.me' in rev  # no longer hardcoded to Streamlabs


def test_bed_roster_keeps_recent_and_prunes_expired(tmp_path: Path) -> None:
    """A donor stays under the bed for the TTL; then is pruned on write."""
    roster = BedRoster(tmp_path / 'bed.json')
    now = 1_000_000.0
    roster.add('Vasya', now)
    roster.add('Alex', now - 1)
    assert roster.active(now) == ['Vasya', 'Alex']  # newest first
    later = now + BED_TTL_SEC + 10
    assert roster.active(later) == []  # both expired
    roster.add('Vasya', later)  # re-donation refreshes and prunes Alex
    assert roster.active(later) == ['Vasya']


def test_render_bed_lists_names_or_says_empty() -> None:
    """The roster render names each donor, or the empty-bed line."""
    templates = load_messages()
    assert render_bed(templates, []) == templates['bed_empty']
    text = render_bed(templates, ['Vasya', ''])
    assert 'Vasya' in text
    assert templates['anonymous'] in text  # '' -> the anonymous label
    assert templates['bed_head'].format(count=2) in text


def test_bed_command_answers_a_trigger_and_stays_silent(
    tmp_path: Path,
) -> None:
    """A bed trigger renders the roster; other chatter gets no reply."""
    roster = bed_roster(tmp_path)
    roster.add('Vasya', time.time())
    cmd = _BedCommand(roster, load_messages())
    assert 'Vasya' in cmd('/bed')  # ASCII trigger
    ru_trigger = load_messages()['bed_triggers'].split('|')[0]
    assert 'Vasya' in cmd('hi ' + ru_trigger)  # a Russian trigger, embedded
    assert cmd('hello there') == ''  # non-trigger stays silent
