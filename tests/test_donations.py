"""donations bot: feed parsing, Russian render, and clean idle.

Hermetic: no network, no tokens. Streamlabs parsing is exercised on a
fixed payload; the poll loop on feed/sender doubles.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from minion_core.adapters.donations import AlertSpec
from minion_core.adapters.donations import DeadFeed
from minion_core.adapters.donations import Donation
from minion_core.adapters.donations import DonationAlerts
from minion_core.adapters.donations import StreamlabsFeed
from minion_core.adapters.donations import _parse
from minion_core.adapters.donations import feed_for
from minions.donations.main import build
from minions.donations.main import load_messages
from minions.donations.main import main
from minions.donations.main import render
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


def test_feed_for_selects_streamlabs_and_degrades() -> None:
    """Known+token is live; unknown or tokenless idles cleanly."""
    live = feed_for('streamlabs', {'STREAMLABS_TOKEN': 'x'})
    assert isinstance(live, StreamlabsFeed)
    assert live.live
    assert live.name == 'Streamlabs'
    other = feed_for('revolut', {})
    assert isinstance(other, DeadFeed)
    assert not other.live  # future platform, not wired yet -> idle
    assert not feed_for('streamlabs', {}).live  # tokenless -> idle


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
        offset=cfg.state / 'donations.offset',
        render=lambda a: f'{a.name}:{a.amount}',
    )
    src = DonationAlerts(feed, sender, spec)

    assert src.drain_once(0) == 2
    assert sender.sent == [('@chan', 'A:1'), ('@chan', 'B:2')]
    assert src.drain_once(2) == 2  # nothing newer
    assert len(sender.sent) == 2  # no duplicate posts


def test_build_and_main_idle_without_config(tmp_path: Path) -> None:
    """REQ-DEG-001: unconfigured, the graph drains clean and exits 0."""
    cfg = make_cfg(tmp_path / 'drive')
    graph = build(cfg, {})
    assert list(graph(iter(()))) == []  # tokenless: drains clean
    assert main(make_env(tmp_path / 'drive')) == 0
