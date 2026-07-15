"""progress_style: pluggable, themeable, ASCII-source progress rendering."""

from __future__ import annotations

from minions.telegram.progress_style import DONE
from minions.telegram.progress_style import DOWNLOADING
from minions.telegram.progress_style import ERROR
from minions.telegram.progress_style import SENDING
from minions.telegram.progress_style import STYLES
from minions.telegram.progress_style import BarStyle
from minions.telegram.progress_style import checklist
from minions.telegram.progress_style import checklist_error
from minions.telegram.progress_style import style_for


def test_every_theme_renders_all_phases() -> None:
    """Each registered theme yields a non-empty line for every phase."""
    phases = (DOWNLOADING, SENDING, DONE, ERROR)
    for style in STYLES.values():
        for phase in phases:
            for pct in (0, 50, 100):
                assert style.render(phase, pct).strip()


def test_bar_grows_with_percent_and_shows_percent() -> None:
    """A bar theme fills up and prints the percent."""
    style = STYLES['blocks']
    assert '0%' in style.render(DOWNLOADING, 0)
    assert '100%' in style.render(DOWNLOADING, 100)
    fill = STYLES['blocks'].render(DOWNLOADING, 100)
    half = STYLES['blocks'].render(DOWNLOADING, 50)
    # More of the fill glyph at 100% than at 50%.
    glyph = chr(0x25B0)
    assert fill.count(glyph) > half.count(glyph)


def test_dopamine_caption_climbs_by_milestone() -> None:
    """The caption is the highest milestone reached."""
    style = STYLES['blocks']
    assert 'just started' in style.render(DOWNLOADING, 0)
    assert 'halfway' in style.render(DOWNLOADING, 50)
    assert 'almost there' in style.render(DOWNLOADING, 85)


def test_bar_clamps_out_of_range_percent() -> None:
    """An extractor over/undershoot never breaks the bar."""
    style = BarStyle(fill=chr(0x25B0), empty=chr(0x25B1), segments=10)
    assert '0%' in style.render(DOWNLOADING, -5)
    assert '100%' in style.render(DOWNLOADING, 250)


def test_checklist_accumulates_completed_steps() -> None:
    """The list grows: received stays, later steps mark done."""
    style = STYLES['blocks']
    dl = checklist(style, DOWNLOADING, 40)
    assert 'Link received' in dl
    assert '40%' in dl  # the live bar is on the downloading line
    done = checklist(style, DONE, 100)
    # By done, every earlier step is present and checked off.
    assert 'Link received' in done
    assert 'Downloaded' in done
    assert 'Sent' in done
    assert done.count('\n') >= 3  # a multi-line checklist, not one line


def test_checklist_error_keeps_the_first_step_and_shows_why() -> None:
    """A failure keeps 'received' and appends the reason -- never silent."""
    text = checklist_error('the service is offline')
    assert 'Link received' in text
    assert 'the service is offline' in text


def test_style_for_picks_by_env_and_falls_back() -> None:
    """RELAY_PROGRESS_STYLE selects a theme; unknown -> the default."""
    assert style_for({'RELAY_PROGRESS_STYLE': 'donut'}) is STYLES['donut']
    assert style_for({'RELAY_PROGRESS_STYLE': 'nope'}) is STYLES['blocks']
    assert style_for({}) is STYLES['blocks']
