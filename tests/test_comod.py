# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Cabinet roster tests: the 7-day shelf TTL, labels, and announcements."""

from __future__ import annotations

from typing import TYPE_CHECKING

from minions.aggregator import comod

if TYPE_CHECKING:
    from pathlib import Path

DAY = 24 * 3600
# 'Dasha' in Cyrillic, spelled with \u escapes so this test source is ASCII.
_DASHA = '\u0414\u0430\u0448\u0430'  # Cyrillic 'Dasha'


def _roster(tmp_path: Path) -> comod.CabinetRoster:
    return comod.CabinetRoster(tmp_path / 'comod.json')


def test_add_then_active_returns_nick_and_amount(tmp_path: Path) -> None:
    """A move-in shows up as (nick, amount)."""
    roster = _roster(tmp_path)
    roster.add('Nick_01', '50', 1000.0)
    assert roster.active(1001.0) == [('Nick_01', '50')]


def test_expires_after_seven_days(tmp_path: Path) -> None:
    """A shelf frees up ('moved out') once the 7-day timer passes."""
    roster = _roster(tmp_path)
    roster.add('Nick_01', '50', 1000.0)
    assert roster.active(1000.0 + 6 * DAY) == [('Nick_01', '50')]
    assert roster.active(1000.0 + 8 * DAY) == []


def test_readd_refreshes_timer_and_amount(tmp_path: Path) -> None:
    """Re-seating a nick renews its week and updates the amount."""
    roster = _roster(tmp_path)
    roster.add('Nick_01', '50', 1000.0)
    roster.add('Nick_01', '90', 1000.0 + 6 * DAY)
    assert roster.active(1000.0 + 8 * DAY) == [('Nick_01', '90')]


def test_most_recent_move_in_first(tmp_path: Path) -> None:
    """``active`` is ordered newest move-in first."""
    roster = _roster(tmp_path)
    roster.add('First', '1', 1000.0)
    roster.add('Second', '2', 2000.0)
    assert roster.active(2001.0) == [('Second', '2'), ('First', '1')]


def test_at_prefix_is_stripped(tmp_path: Path) -> None:
    """A leading '@' on the nick is normalized away."""
    roster = _roster(tmp_path)
    roster.add('@Bob', '10', 1000.0)
    assert roster.active(1001.0) == [('Bob', '10')]


def test_remove_evicts(tmp_path: Path) -> None:
    """``remove`` evicts by hand and reports whether the nick was there."""
    roster = _roster(tmp_path)
    roster.add('Bob', '10', 1000.0)
    assert roster.remove('@Bob') is True
    assert roster.active(1001.0) == []
    assert roster.remove('Ghost') is False


def test_cyrillic_nick_round_trips(tmp_path: Path) -> None:
    """A Cyrillic nick survives the JSON round-trip intact."""
    roster = _roster(tmp_path)
    roster.add(_DASHA, '30', 1000.0)
    assert roster.active(1001.0) == [(_DASHA, '30')]


def test_blank_nick_is_ignored(tmp_path: Path) -> None:
    """A whitespace-only nick is not seated."""
    roster = _roster(tmp_path)
    roster.add('   ', '10', 1000.0)
    assert roster.active(1001.0) == []


def test_load_tolerates_missing_and_corrupt(tmp_path: Path) -> None:
    """No file, or a corrupt file, reads as an empty cabinet."""
    roster = _roster(tmp_path)
    assert roster.active(1.0) == []
    (tmp_path / 'comod.json').write_text('not json', encoding='utf-8')
    assert roster.active(1.0) == []


def test_labels_for_stacks_amount_under_nick() -> None:
    """The amount sits on its own line under the nick; else nick only."""
    labels = comod.labels_for([('Nick_01', '50'), ('Bob', '')])
    assert labels == ['Nick_01\n$50', 'Bob']


def test_labels_for_dedups_a_typed_dollar() -> None:
    """A '$' the operator already typed is not doubled."""
    assert comod.labels_for([('Bob', '$40')]) == ['Bob\n$40']


def test_load_comod_params_defaults() -> None:
    """An absent 'comod' section yields the prototype's 10-shelf layout."""
    params = comod.load_comod_params({})
    assert params.max_shelves == len(params.slots) == 10
    assert params.text_color == (255, 255, 255)
    assert params.slots[0] == (440, 73, 325, 206)


def test_load_comod_params_reads_section() -> None:
    """Configured knobs override the defaults."""
    params = comod.load_comod_params(
        {
            'comod': {
                'donate_link': 'http://d',
                'render': {
                    'template': 't.jpg',
                    'max_shelves': 3,
                    'slots': [[1, 2, 3, 4]],
                    'text_color': [1, 2, 3],
                },
            }
        }
    )
    assert params.donate_link == 'http://d'
    assert params.template_path == 't.jpg'
    assert params.slots == ((1, 2, 3, 4),)
    assert params.max_shelves == 3
    assert params.text_color == (1, 2, 3)


def test_by_amount_orders_biggest_first() -> None:
    """Residents rank by donated amount, largest first; non-numeric = 0."""
    residents = [
        ('Small', '5'),
        ('Big', '$100'),
        ('Zero', 'aa'),
        ('Mid', '40'),
    ]
    assert comod.by_amount(residents) == [
        ('Big', '$100'),
        ('Mid', '40'),
        ('Small', '5'),
        ('Zero', 'aa'),
    ]


def test_assign_labels_fills_top_to_bottom_by_amount() -> None:
    """Donors fill slots in listed (top-to-bottom) order, biggest first."""
    slots = (
        (0, 0, 10, 10),  # slot 0 (top)
        (0, 0, 20, 20),  # slot 1
        (0, 0, 40, 40),  # slot 2 (largest area, but LAST in the list)
    )
    residents = [('A', '5'), ('B', '100'), ('C', '40')]  # by recency
    labels = comod.assign_labels(residents, slots)
    # Ranked by amount B>C>A, placed straight down the list (area ignored).
    assert labels == ['B\n$100', 'C\n$40', 'A\n$5']


def test_assign_labels_leaves_spare_slots_blank() -> None:
    """Fewer residents than slots leaves the smaller shelves empty."""
    slots = ((0, 0, 40, 40), (0, 0, 10, 10))
    labels = comod.assign_labels([('Big', '90')], slots)
    assert labels == ['Big\n$90', '']  # top donor on slot 0, spare blank


def test_move_in_text_fills_placeholders() -> None:
    """{nick} (without '@') and {link} are substituted into the template."""
    templates = {'move_in': '@{nick} welcome -> {link}'}
    result = comod.move_in_text(templates, '@Bob', 'http://d')
    assert result == '@Bob welcome -> http://d'


def test_move_in_text_picks_from_a_variant_list() -> None:
    """A list of variants is supported (a single entry is deterministic)."""
    templates = {'move_in': ['only {nick}']}
    assert comod.move_in_text(templates, 'Bob', 'x') == 'only Bob'
