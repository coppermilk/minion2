"""progress_style: label + fixed bar + detail, themeable, ASCII source."""

from __future__ import annotations

from minion_core.progress import Report
from minions.telegram.progress_style import DONE
from minions.telegram.progress_style import DOWNLOADING
from minions.telegram.progress_style import ERROR
from minions.telegram.progress_style import RECEIVED
from minions.telegram.progress_style import SENDING
from minions.telegram.progress_style import STYLES
from minions.telegram.progress_style import style_for


def test_downloading_shows_label_bar_percent_and_detail() -> None:
    """The download block: label, bar+percent, then MB / ETA."""
    style = STYLES['blocks']
    text = style.render(DOWNLOADING, Report(52, 24_800_000, 47_600_000, 12))
    lines = text.split('\n')
    assert lines[0] == 'downloading'
    assert '52%' in lines[1]
    assert '24.8 / 47.6 MB' in lines[2]
    assert '12s left' in lines[2]


def test_received_and_sending_show_percent() -> None:
    """The first and upload states show the percent, no detail."""
    style = STYLES['blocks']
    assert style.render(RECEIVED, Report(0)).startswith('link received\n')
    assert '0%' in style.render(RECEIVED, Report(0))
    assert '100%' in style.render(SENDING, Report(100))


def test_done_shows_the_detail_in_place_of_percent() -> None:
    """Done shows the size/elapsed line, not a percent."""
    text = STYLES['blocks'].render(DONE, Report(100), '47.6 MB . 47s')
    assert text.split('\n')[0] == 'done'
    assert '47.6 MB' in text
    assert '%' not in text  # the detail replaces the percent


def test_error_shows_the_reason() -> None:
    """An error block shows the reason under the label."""
    text = STYLES['blocks'].render(ERROR, Report(0), 'service offline')
    assert 'service offline' in text


def test_bar_fills_with_the_percent() -> None:
    """More of the fill glyph at 100% than at 0%."""
    glyph = chr(0x2588)
    full = STYLES['blocks'].render(SENDING, Report(100))
    empty = STYLES['blocks'].render(RECEIVED, Report(0))
    assert full.count(glyph) > empty.count(glyph)


def test_style_for_picks_by_env_and_falls_back() -> None:
    """RELAY_PROGRESS_STYLE selects a theme; unknown -> the default."""
    assert style_for({'RELAY_PROGRESS_STYLE': 'shade'}) is STYLES['shade']
    assert style_for({'RELAY_PROGRESS_STYLE': 'nope'}) is STYLES['blocks']
    assert style_for({}) is STYLES['blocks']
