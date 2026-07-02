"""fan-save bot tests: link -> video parked in the fan queue."""

from __future__ import annotations

from typing import TYPE_CHECKING

import minions.fan_save.main
from minion_core.adapters import fetch
from minion_core.adapters.fetch import FetchLink
from minion_core.adapters.tg import TgApi
from minion_core.adapters.tg import TgChannel
from minion_core.adapters.tg import spool_of
from minion_core.kernel import ArchiveTo
from minion_core.kernel import DisposeSource
from minion_core.kernel import Disposition
from minion_core.kernel import Envelope
from minion_core.kernel import Job
from minion_core.kernel import Origin
from minion_core.kernel import Reply
from minions.fan_save.main import build
from tests.conftest import make_cfg
from tests.conftest import make_env

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from minion_core.settings import Settings


def test_tokenless_fan_save_runs(tmp_path: Path) -> None:
    """REQ-DEG-001: the bot runs clean without a token."""
    make_cfg(tmp_path / 'drive')
    assert minions.fan_save.main.main(make_env(tmp_path / 'drive')) == 0
    assert build(make_cfg(tmp_path / 'drive'), {'TG_TOKEN': ''}) is not None


def test_link_lands_in_the_fan_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spooled link ends up as a file in bots/fan-save/done/.

    The belt tail is exercised directly (the bot's exact stages in
    the bot's exact order); the Telegram dock is covered by the
    shared source tests.
    """
    cfg = make_cfg(tmp_path / 'drive')

    def fake_download(url: str, into: Path, _cfg: Settings) -> Path:
        got = into / 'clip.mp4'
        got.parent.mkdir(parents=True, exist_ok=True)
        got.write_bytes(b'video')
        return got

    monkeypatch.setattr(fetch, 'download', fake_download)
    spool = cfg.bot_dir('fan-save') / 'link.url'
    spool.parent.mkdir(parents=True, exist_ok=True)
    spool.write_text('https://example.com/v', encoding='ascii')
    job = Job(
        src=spool,
        dest=cfg.bot_dir('fan-save'),
        stem='link',
        origin=Origin('tg', f'1:2:{spool}'),
    )
    tail = (
        FetchLink(cfg)
        >> ArchiveTo(cfg.bot_done('fan-save'))
        >> Reply(TgChannel(TgApi('')))
        >> DisposeSource(spool_of)
    )
    out = list(tail(iter([Envelope(job)])))
    assert len(out) == 1
    verdict = out[0].verdict
    assert verdict is not None
    assert verdict.disposition is Disposition.DELIVERED
    assert (cfg.bot_done('fan-save') / 'clip.mp4').is_file()
    assert not spool.exists()  # spool disposed after delivery
