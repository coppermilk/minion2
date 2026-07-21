"""wishlist bot: HTML parsing, the gift diff, snapshots and one run.

Hermetic: no network. The wishlist page is a fixed fixture; the run
loop uses fetch/post doubles.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from minion_core.adapters.wishlist import SnapshotStore
from minion_core.adapters.wishlist import WishItem
from minion_core.adapters.wishlist import gifted
from minion_core.adapters.wishlist import parse_items
from minions.wishlist.main import Deps
from minions.wishlist.main import Spec
from minions.wishlist.main import load_messages
from minions.wishlist.main import main
from minions.wishlist.main import render
from minions.wishlist.main import run_once
from tests.conftest import make_cfg
from tests.conftest import make_env

if TYPE_CHECKING:
    from pathlib import Path

_PAGE = """
<ul>
<li data-id="a" data-itemid="I1AAA">
  <img alt="p" src="https://m.media-amazon.com/images/I/71aaa._AC_SL200_.jpg">
  <a id="itemName_I1AAA" class="a-link-normal itemName"
     title="Cool Gadget 3000" href="/dp/B0AAA">Cool Gadget...</a>
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


def test_parse_items_extracts_id_title_and_image() -> None:
    """Each item yields its id, unescaped title and product image."""
    items = parse_items(_PAGE)
    assert [i.ident for i in items] == ['I1AAA', 'I2BBB']  # third skipped
    assert items[0].title == 'Cool Gadget 3000'
    assert items[0].image.endswith('71aaa._AC_SL200_.jpg')
    assert items[1].title == 'Fancy Mug & Co'  # &amp; unescaped


def test_parse_items_empty_page_is_empty() -> None:
    """No item markers -> no items (the caller treats it as a failure)."""
    assert parse_items('<html>nothing here</html>') == []


def test_gifted_is_what_left_the_list() -> None:
    """Present yesterday, gone today -> gifted; new arrivals ignored."""
    a = WishItem('I1', 'A', '')
    b = WishItem('I2', 'B', '')
    c = WishItem('I3', 'C', '')
    left = gifted([a, b], [a, c])  # b gone, c is new
    assert [i.ident for i in left] == ['I2']


def test_snapshot_roundtrips_including_unicode(tmp_path: Path) -> None:
    """Save then load returns the same items; unicode survives."""
    path = tmp_path / 'wishlist.json'
    store = SnapshotStore(path)
    assert store.load() == []  # missing file -> empty
    title = 'Mug ' + chr(0x20BD)  # a non-ASCII title, built without a literal
    items = [WishItem('I1', title, 'https://img/1.jpg')]
    store.save(items)
    assert store.load() == items


def test_render_caption_names_item_and_link() -> None:
    """The thank-you names the gifted item and links the live list."""
    templates = load_messages()
    item = WishItem('I1', 'Cool Gadget 3000', 'https://img/1.jpg')
    caption = render(templates, item, 'https://amazon.de/wishlist/X')
    assert 'Cool Gadget 3000' in caption
    assert 'https://amazon.de/wishlist/X' in caption
    assert not caption.isascii()  # the Russian template is exercised


class _Poster:
    """Records (image, caption) pairs instead of calling Telegram."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, str]] = []

    def post(self, image: str, caption: str) -> None:
        self.posts.append((image, caption))


def _spec() -> Spec:
    return Spec(url='https://amazon.de/wishlist/X', chat='@chan')


def test_first_run_only_snapshots(tmp_path: Path) -> None:
    """With no prior snapshot, nothing is gifted; a baseline is saved."""
    cfg = make_cfg(tmp_path / 'drive')
    item = WishItem('I1', 'Gadget', 'https://img/1.jpg')
    poster = _Poster()
    deps = Deps(fetch=lambda _url: [item], post=poster.post)
    run_once(cfg, _spec(), deps)
    assert poster.posts == []  # nothing to compare against yet
    assert SnapshotStore(cfg.state / 'wishlist.json').load() == [item]


def test_run_reports_the_vanished_item(tmp_path: Path) -> None:
    """An item gone since yesterday posts its photo and thank-you."""
    cfg = make_cfg(tmp_path / 'drive')
    kept = WishItem('I1', 'Gadget', 'https://img/1.jpg')
    gone = WishItem('I2', 'Cool Mug', 'https://img/2.jpg')
    SnapshotStore(cfg.state / 'wishlist.json').save([kept, gone])
    poster = _Poster()
    deps = Deps(fetch=lambda _url: [kept], post=poster.post)  # I2 vanished
    run_once(cfg, _spec(), deps)
    assert len(poster.posts) == 1
    image, caption = poster.posts[0]
    assert image == 'https://img/2.jpg'
    assert 'Cool Mug' in caption
    assert SnapshotStore(cfg.state / 'wishlist.json').load() == [kept]


def test_fetch_failure_keeps_snapshot_and_stays_silent(
    tmp_path: Path,
) -> None:
    """A None fetch (block/error) posts nothing and never re-snapshots."""
    cfg = make_cfg(tmp_path / 'drive')
    before = [WishItem('I1', 'A', ''), WishItem('I2', 'B', '')]
    SnapshotStore(cfg.state / 'wishlist.json').save(before)
    poster = _Poster()
    deps = Deps(fetch=lambda _url: None, post=poster.post)  # blocked
    run_once(cfg, _spec(), deps)
    assert poster.posts == []  # no false "everything gifted" storm
    assert SnapshotStore(cfg.state / 'wishlist.json').load() == before


def test_idle_without_url_or_chat(tmp_path: Path) -> None:
    """Unconfigured, run_once and main are clean no-ops (exit 0)."""
    cfg = make_cfg(tmp_path / 'drive')
    poster = _Poster()
    deps = Deps(fetch=lambda _url: [WishItem('I1', 'A', '')], post=poster.post)
    run_once(cfg, Spec(url='', chat=''), deps)
    assert poster.posts == []
    assert main(make_env(tmp_path / 'drive')) == 0
