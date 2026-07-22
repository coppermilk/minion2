"""moderator bot: the command handler flips the toggle and cleans."""

from __future__ import annotations

import time

from minion_core.adapters.backend import BackendToggle
from minion_core.adapters.donations import bed_roster
from minion_core.adapters.wishlist import SnapshotStore
from minion_core.adapters.wishlist import WishItem
from minion_core.kernel import bot_logger
from minions.bots.model_switch.main import _MENU
from minions.bots.model_switch.main import _Moderator
from minions.bots.model_switch.main import reply_for
from tests.conftest import make_cfg


def _handler(cfg):
    log = bot_logger('model-switch', cfg.logs)
    return _Moderator(cfg, BackendToggle(cfg), log)


def _classified_jpeg(cfg, name):
    from PIL import Image

    from minion_core.adapters.files import tag_fandom
    from minion_core.adapters.files import tag_week

    img = cfg.inbox / name
    img.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (32, 32), (250, 250, 250)).save(img, 'JPEG')
    tag_fandom(img, 'HarryPotter')
    tag_week(img, cfg.week_tag)
    return img


def test_switch_flips_and_reports(tmp_path):
    """local/gemini set the toggle; status reads it back."""
    cfg = make_cfg(tmp_path / 'drive')
    toggle = BackendToggle(cfg)
    assert 'gemini' in reply_for(toggle, 'status')  # default
    assert reply_for(toggle, ' LOCAL ') == 'backend set to local'
    assert toggle.read() == 'local'
    assert reply_for(toggle, 'gemini') == 'backend set to gemini'
    assert toggle.read() == 'gemini'


def test_switch_unknown_gives_help(tmp_path):
    """A stray word lists the accepted commands, changes nothing."""
    cfg = make_cfg(tmp_path / 'drive')
    reply = reply_for(BackendToggle(cfg), 'wut')
    assert 'local' in reply
    assert 'gemini' in reply
    assert 'clean' in reply  # the on-demand clean is advertised too
    assert 'menu' in reply  # the panel is discoverable
    assert BackendToggle(cfg).read() == 'gemini'  # unchanged (default)


def test_panel_shows_menu_and_reads_bot_status(tmp_path):
    """The admin panel prints the menu and reads donations/wishlist state."""
    cfg = make_cfg(tmp_path / 'drive')
    mod = _handler(cfg)
    assert mod('menu') == _MENU
    assert mod('help') == _MENU
    assert 'empty' in mod('bed')  # no donors under the bed yet
    bed_roster(cfg.state).add('Vasya', time.time())
    assert 'Vasya' in mod('bed')  # now one is
    assert '0 items' in mod('wishlist')  # nothing tracked yet
    SnapshotStore(cfg.state / 'wishlist.json').save([WishItem('I1', 'X', '')])
    assert '1 items' in mod('wishlist')


def test_clean_command_shelves_the_week(tmp_path):
    """`clean` runs the week-clean shelving on demand, right now."""
    cfg = make_cfg(tmp_path / 'drive')
    img = _classified_jpeg(cfg, 'FgSnapeOfficeAngry.jpg')
    reply = _handler(cfg)('clean')
    assert 'cleaning' in reply.lower()
    shelved = cfg.pictures / 'HarryPotter' / 'FgSnapeOfficeAngry.jpg'
    assert shelved.exists()
    assert not img.exists()


def test_clean_command_reports_when_a_clean_is_running(tmp_path):
    """A held batch lock means the command says so, shelves nothing."""
    from minion_core.adapters.files import BatchLock
    from minion_core.adapters.library import LOCK_NAME

    cfg = make_cfg(tmp_path / 'drive')
    img = _classified_jpeg(cfg, 'FgSnapeOfficeAngry.jpg')
    lock = BatchLock(cfg.state / LOCK_NAME)
    assert lock.acquire()
    try:
        reply = _handler(cfg)('clean')
    finally:
        lock.release()
    assert 'already running' in reply
    assert img.exists()  # nothing shelved while another run holds the lock
