"""wishlist bot: HTML parsing, lek pagination, the gift diff, one run.

Hermetic: no network. The wishlist page is a fixed fixture; the run
loop uses fetch/post/say doubles and a monkeypatched page fetcher.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from minion_core.adapters import wishlist as wl
from minion_core.adapters.wishlist import SnapshotStore
from minion_core.adapters.wishlist import WishItem
from minion_core.adapters.wishlist import _next_lek
from minion_core.adapters.wishlist import fetch_items
from minion_core.adapters.wishlist import gifted
from minion_core.adapters.wishlist import parse_items
from minions.bots.wishlist.main import Deps
from minions.bots.wishlist.main import Spec
from minions.bots.wishlist.main import digest
from minions.bots.wishlist.main import load_messages
from minions.bots.wishlist.main import main
from minions.bots.wishlist.main import render
from minions.bots.wishlist.main import run_once
from tests.conftest import make_cfg
from tests.conftest import make_env

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_PAGE = """
<ul>
<li data-id="a" data-itemid="I1AAA">
  <img alt="p" src="https://m.media-amazon.com/images/I/71aaa._AC_SL200_.jpg">
  <a id="itemName_I1AAA" class="a-link-normal itemName"
     title="Cool Gadget 3000" href="/dp/B0AAA">Cool Gadget...</a>
  <span id="itemComment_I1AAA">also love mint &amp; red</span>
</li>
<li data-id="b" data-itemid="I2BBB">
  <img alt="p" src="https://m.media-amazon.com/images/I/81bbb._AC_SL200_.jpg">
  <a id="itemName_I2BBB" class="a-link-normal itemName"
     title="Fancy Mug &amp; Co" href="/dp/B0BBB">Fancy Mug...</a>
</li>
<li data-id="c" data-itemid="I3CCC">
  <span>no title anchor here, should be skipped</span>
</li>
</ul>
"""


def test_parse_items_extracts_id_title_image_and_note() -> None:
    """Each item yields id, unescaped title, image and the owner note."""
    items = parse_items(_PAGE)
    assert [i.ident for i in items] == ['I1AAA', 'I2BBB']  # third skipped
    assert items[0].title == 'Cool Gadget 3000'
    assert items[0].image.endswith('71aaa._AC_SL200_.jpg')
    assert items[0].note == 'also love mint & red'  # &amp; unescaped
    assert items[1].title == 'Fancy Mug & Co'
    assert items[1].note == ''  # no comment span -> empty


def test_parse_items_empty_page_is_empty() -> None:
    """No item markers -> no items (the caller treats it as a failure)."""
    assert parse_items('<html>nothing here</html>') == []


def test_next_lek_reads_the_pagination_token() -> None:
    """The lek token is read from a show-more URL or the JSON key."""
    assert _next_lek('data-showmoreurl="/m?lek=AB%3DC&z=1"') == 'AB=C'
    assert _next_lek('"lastEvaluatedKey":"XYZ"') == 'XYZ'
    assert _next_lek('nothing here') is None


def test_fetch_walks_lek_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pages are walked via lek and their items concatenated, deduped."""
    page1 = (
        '<li data-itemid="I1"><a id="itemName_I1" title="A"></a></li>'
        'data-showmoreurl="/hz/more?lek=TOK2&x=1"'
    )
    page2 = '<li data-itemid="I2"><a id="itemName_I2" title="B"></a></li>'
    pages = {None: page1, 'TOK2': page2}
    monkeypatch.setattr(wl, '_get_page', lambda _url, lek: pages.get(lek))
    items = fetch_items('http://x')
    assert items is not None
    assert [i.ident for i in items] == ['I1', 'I2']


def test_fetch_fails_whole_run_if_a_page_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later page that fails to load fails the run (no partial list)."""
    page1 = (
        '<li data-itemid="I1"><a id="itemName_I1" title="A"></a></li>'
        'data-showmoreurl="/m?lek=TOK2"'
    )
    monkeypatch.setattr(
        wl, '_get_page', lambda _url, lek: page1 if lek is None else None
    )
    assert fetch_items('http://x') is None  # partial would cause false gifts


def test_gifted_is_what_left_the_list() -> None:
    """Present yesterday, gone today -> gifted; new arrivals ignored."""
    a = WishItem('I1', 'A', '')
    b = WishItem('I2', 'B', '')
    c = WishItem('I3', 'C', '')
    left = gifted([a, b], [a, c])  # b gone, c is new
    assert [i.ident for i in left] == ['I2']


def test_snapshot_roundtrips_note_and_unicode(tmp_path: Path) -> None:
    """Save then load returns the same items; note and unicode survive."""
    path = tmp_path / 'wishlist.json'
    store = SnapshotStore(path)
    assert store.load() == []  # missing file -> empty
    title = 'Mug ' + chr(0x20BD)  # non-ASCII title, no literal in source
    note = 'love ' + chr(0x2764)
    items = [WishItem('I1', title, 'https://img/1.jpg', note=note)]
    store.save(items)
    assert store.load() == items


def test_render_trims_title_to_five_words_and_weaves_note() -> None:
    """A long card name is trimmed to five words; the note is woven in."""
    templates = load_messages()
    item = WishItem(
        'I1',
        'Optimum Nutrition Gold Standard Whey Chocolate Peanut',
        'https://img/1.jpg',
        note='also love the chocolate and mint flavour',
    )
    caption = render(templates, item, 'https://amazon.de/wl/X')
    assert 'Optimum Nutrition Gold Standard Whey' in caption  # 5 words
    assert 'Chocolate' not in caption  # the 6th word was trimmed
    assert 'also love the chocolate and mint flavour' in caption  # note
    assert 'https://amazon.de/wl/X' in caption
    assert not caption.isascii()


def test_render_omits_note_line_when_there_is_no_note() -> None:
    """No note -> the note line is absent, just the plain thank-you."""
    templates = load_messages()
    caption = render(templates, WishItem('I1', 'Gadget', 'x'), 'link')
    assert templates['note_line'].strip() not in caption
    assert 'Gadget' in caption


def test_digest_lists_the_current_wishlist(tmp_path: Path) -> None:
    """The digest names each item and reports the count."""
    templates = load_messages()
    items = [WishItem('I1', 'Gadget', 'x'), WishItem('I2', 'Mug', 'y')]
    text = digest(templates, items, 'https://amazon.de/wl/X')
    assert 'Gadget' in text
    assert 'Mug' in text
    assert '2' in text  # the count
    assert 'https://amazon.de/wl/X' in text


class _Sink:
    """Records photo posts and text messages instead of calling Telegram."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, str]] = []
        self.says: list[str] = []

    def post(self, image: str, caption: str) -> None:
        self.posts.append((image, caption))

    def say(self, message: str) -> None:
        self.says.append(message)


def _deps(sink: _Sink, result: list[WishItem] | None) -> Deps:
    return Deps(fetch=lambda _url: result, post=sink.post, say=sink.say)


def _spec(*, announce: bool = False) -> Spec:
    return Spec(url='https://amazon.de/wl/X', chat='@chan', announce=announce)


def test_first_run_announces_the_list_and_snapshots(tmp_path: Path) -> None:
    """No prior snapshot: send a digest, gift nothing, save a baseline."""
    cfg = make_cfg(tmp_path / 'drive')
    item = WishItem('I1', 'Gadget', 'https://img/1.jpg')
    sink = _Sink()
    run_once(cfg, _spec(), _deps(sink, [item]))
    assert len(sink.says) == 1  # the wishlist digest
    assert 'Gadget' in sink.says[0]
    assert sink.posts == []  # nothing to compare against yet
    assert SnapshotStore(cfg.state / 'wishlist.json').load() == [item]


def test_run_reports_the_vanished_item(tmp_path: Path) -> None:
    """An item gone since yesterday posts its photo and thank-you."""
    cfg = make_cfg(tmp_path / 'drive')
    kept = WishItem('I1', 'Gadget', 'https://img/1.jpg')
    gone = WishItem('I2', 'Cool Mug', 'https://img/2.jpg', note='in blue')
    SnapshotStore(cfg.state / 'wishlist.json').save([kept, gone])
    sink = _Sink()
    run_once(cfg, _spec(), _deps(sink, [kept]))  # I2 vanished
    assert sink.says == []  # not first run, not forced -> no digest
    assert len(sink.posts) == 1
    image, caption = sink.posts[0]
    assert image == 'https://img/2.jpg'
    assert 'Cool Mug' in caption
    assert 'in blue' in caption  # the note is woven in
    assert SnapshotStore(cfg.state / 'wishlist.json').load() == [kept]


def test_announce_flag_forces_a_digest(tmp_path: Path) -> None:
    """WISHLIST_ANNOUNCE resends the list even with a prior snapshot."""
    cfg = make_cfg(tmp_path / 'drive')
    item = WishItem('I1', 'Gadget', 'https://img/1.jpg')
    SnapshotStore(cfg.state / 'wishlist.json').save([item])
    sink = _Sink()
    run_once(cfg, _spec(announce=True), _deps(sink, [item]))
    assert len(sink.says) == 1  # forced digest
    assert sink.posts == []  # nothing vanished


def test_fetch_failure_keeps_snapshot_and_stays_silent(
    tmp_path: Path,
) -> None:
    """A None fetch (block/error) posts nothing and never re-snapshots."""
    cfg = make_cfg(tmp_path / 'drive')
    before = [WishItem('I1', 'A', ''), WishItem('I2', 'B', '')]
    SnapshotStore(cfg.state / 'wishlist.json').save(before)
    sink = _Sink()
    run_once(cfg, _spec(), _deps(sink, None))  # blocked
    assert sink.posts == []  # no false "everything gifted" storm
    assert sink.says == []
    assert SnapshotStore(cfg.state / 'wishlist.json').load() == before


def test_idle_without_url_or_chat(tmp_path: Path) -> None:
    """Unconfigured, run_once and main are clean no-ops (exit 0)."""
    cfg = make_cfg(tmp_path / 'drive')
    sink = _Sink()
    deps = _deps(sink, [WishItem('I1', 'A', '')])
    run_once(cfg, Spec(url='', chat=''), deps)
    assert sink.posts == []
    assert sink.says == []
    assert main(make_env(tmp_path / 'drive')) == 0
