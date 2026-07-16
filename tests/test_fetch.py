"""fetch adapter tests: REQ-SEC-001, REQ-RES-001, REQ-RES-002."""

from __future__ import annotations

import stat
import subprocess
from typing import TYPE_CHECKING

import pytest

from minion_core.adapters import fetch
from minion_core.adapters.files import QuotaExceeded
from minion_core.kernel import Disposition
from minion_core.kernel import Job
from minion_core.kernel import Origin
from tests.conftest import make_cfg

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Self

    from minion_core.settings import Settings

REJECTED_URLS = (
    'http://127.0.0.1/x',
    'http://localhost/x',
    'http://10.0.0.1/x',
    'http://172.16.5.5/x',
    'http://192.168.1.1/x',
    'http://169.254.169.254/latest/meta-data',
    'http://[::1]/x',
    'http://0.0.0.0/x',
    'http://100.64.0.1/x',
    'file:///etc/passwd',
    'ftp://93.184.216.34/x',
    'http:///nohost',
)


@pytest.mark.parametrize('url', REJECTED_URLS)
def test_ssrf_rejection_table(url: str) -> None:
    """REQ-SEC-001: private/reserved destinations rejected."""
    with pytest.raises(fetch.Blocked, match='ssrf_blocked'):
        fetch.guard(url)


def test_public_literal_ip_passes_guard() -> None:
    """The guard rejects by address class, not by allow-list."""
    fetch.guard('http://93.184.216.34/video')


def _hung_ytdlp(tmp_path: Path) -> Path:
    fake = tmp_path / 'bin' / 'yt-dlp'
    fake.parent.mkdir()
    fake.write_text('#!/bin/sh\nsleep 30\n', encoding='ascii')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake


def test_hung_extractor_hits_wall_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REQ-RES-001: every download is wall-time bounded."""
    cfg = make_cfg(tmp_path / 'drive', DOWNLOAD_TIMEOUT_SEC='1')
    monkeypatch.setattr(fetch, 'YTDLP', str(_hung_ytdlp(tmp_path)))
    with pytest.raises(subprocess.TimeoutExpired):
        fetch.download('http://93.184.216.34/v', cfg.inbox, cfg)


def test_quota_pre_check_rejects_before_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REQ-RES-002: a full tree rejects the download pre-transfer."""
    cfg = make_cfg(tmp_path / 'drive', QUOTA_BYTES='10')
    (cfg.inbox / 'fat.bin').write_bytes(b'x' * 20)

    def no_subprocess(*args: object, **kw: object) -> None:
        raise AssertionError('transfer must not start')

    monkeypatch.setattr(subprocess, 'run', no_subprocess)
    with pytest.raises(QuotaExceeded, match='pre-transfer'):
        fetch.download('http://93.184.216.34/v', cfg.inbox, cfg)


class _FakeResponse:
    """Chunk stream double for the direct-transfer path."""

    def __init__(self, total: int) -> None:
        self._left = total

    def read(self, n: int) -> bytes:
        take = min(n, self._left)
        self._left -= take
        return b'x' * take

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def test_direct_transfer_aborts_mid_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REQ-RES-002: the mid-stream check aborts a direct transfer."""
    cfg = make_cfg(tmp_path / 'drive', QUOTA_BYTES='1000')

    class _Opener:
        def open(self, url: str, timeout: int) -> _FakeResponse:
            return _FakeResponse(total=5000)

    monkeypatch.setattr(
        fetch.urllib.request, 'build_opener', lambda *h: _Opener()
    )
    target = cfg.bot_dir('fetch') / 'direct.bin'
    with pytest.raises(QuotaExceeded):
        fetch.download_direct('http://93.184.216.34/f', target, cfg)
    assert not target.exists()
    assert list(target.parent.glob('*.part')) == []


def _url_job(cfg: Settings, url: str) -> Job:
    spool = cfg.bot_dir('fetch') / 'link.url'
    spool.parent.mkdir(parents=True, exist_ok=True)
    spool.write_text(url, encoding='ascii')
    return Job(
        src=spool,
        dest=cfg.bot_dir('fetch'),
        stem='link',
        origin=Origin('tg', f'1:2:{spool}'),
    )


def test_fetch_link_maps_ssrf_to_rejected(tmp_path: Path) -> None:
    """The step converts Blocked into the stable reason code."""
    cfg = make_cfg(tmp_path / 'drive')
    verdict = fetch.FetchLink(cfg).process(
        _url_job(cfg, 'http://127.0.0.1/x'),
    )
    assert verdict.disposition is Disposition.REJECTED
    assert verdict.reason == 'ssrf_blocked'


def test_fetch_link_maps_timeout_to_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """download_timeout is FAILED: transient, retry may help."""
    cfg = make_cfg(tmp_path / 'drive', DOWNLOAD_TIMEOUT_SEC='1')
    monkeypatch.setattr(fetch, 'YTDLP', str(_hung_ytdlp(tmp_path)))
    verdict = fetch.FetchLink(cfg).process(
        _url_job(cfg, 'http://93.184.216.34/v'),
    )
    assert verdict.disposition is Disposition.FAILED
    assert verdict.reason == 'download_timeout'


def test_fetch_link_passes_media_through(tmp_path: Path) -> None:
    """Non-.url inputs flow untouched (frames' mixed belt)."""
    cfg = make_cfg(tmp_path / 'drive')
    video = cfg.bot_dir('frames') / 'clip.mp4'
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b'v')
    job = Job(
        src=video,
        dest=video.parent,
        stem='clip',
        origin=Origin('tg', f'1:2:{video}'),
    )
    verdict = fetch.FetchLink(cfg).process(job)
    assert verdict.disposition is Disposition.DELIVERED
    assert verdict.result == video


def test_stale_extractor_is_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing extractor maps to stale_extractor (OPERATIONS 2)."""
    cfg = make_cfg(tmp_path / 'drive')
    fake = tmp_path / 'bin' / 'yt-dlp'
    fake.parent.mkdir()
    fake.write_text('#!/bin/sh\necho broken >&2\nexit 1\n', encoding='ascii')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(fetch, 'YTDLP', str(fake))
    verdict = fetch.FetchLink(cfg).process(
        _url_job(cfg, 'http://93.184.216.34/v'),
    )
    assert verdict.disposition is Disposition.FAILED
    assert verdict.reason == 'stale_extractor'


_PROGRESS_YTDLP = (
    '#!/usr/bin/env python3\n'
    'import sys, os\n'
    'a = sys.argv\n'
    'into = a[a.index("-P") + 1]\n'
    'path = os.path.join(into, "video.mp4")\n'
    'open(path, "wb").write(b"video")\n'
    'sys.stderr.write("PROGRESS:  10.0%;100;1000;9\\n")\n'
    'sys.stderr.write("PROGRESS:  55.5%;555;1000;4\\n")\n'
    'sys.stderr.write("PROGRESS: 100%;1000;1000;0\\n")\n'
    'sys.stderr.flush()\n'
    'print(path)\n'
)
"""A fake yt-dlp: PROGRESS:pct;done;total;eta to stderr, the path to stdout."""


def test_download_streams_progress_to_the_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """yt-dlp's percent, bytes and ETA reach the progress sink, live."""
    from minion_core import progress

    cfg = make_cfg(tmp_path / 'drive')
    fake = tmp_path / 'bin' / 'yt-dlp'
    fake.parent.mkdir()
    fake.write_text(_PROGRESS_YTDLP, encoding='ascii')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(fetch, 'YTDLP', str(fake))
    seen: list[progress.Report] = []
    with progress.reporting_to(seen.append):
        got = fetch.download(
            'http://93.184.216.34/v', cfg.bot_dir('fetch'), cfg
        )
    assert got.name == 'video.mp4'
    assert [r.pct for r in seen] == [10, 55, 100]  # parsed, in order
    assert seen[0].done_bytes == 100
    assert seen[0].total_bytes == 1000
    assert seen[0].eta_sec == 9


def test_argv_forces_progress_output(tmp_path: Path) -> None:
    """--progress makes yt-dlp emit percents even without a TTY (container)."""
    cfg = make_cfg(tmp_path / 'drive')
    argv = fetch._argv('http://example.com/v', tmp_path, cfg)
    assert '--progress' in argv  # else the live bar never animates
    assert '--progress-template' in argv


def test_youtube_gets_player_client_args(tmp_path: Path) -> None:
    """The player_client arg attaches only on YouTube hosts."""
    cfg = make_cfg(tmp_path / 'drive', YTDLP_PLAYER_CLIENTS='web,android')
    yt = fetch._argv('https://youtu.be/x', tmp_path, cfg)
    other = fetch._argv('https://example.com/x', tmp_path, cfg)
    assert 'youtube:player_client=web,android' in yt
    assert not any('player_client' in a for a in other)
    assert '--no-playlist' in yt


def test_used_bytes_is_zero_on_empty(tmp_path: Path) -> None:
    """Quota accounting starts at zero."""
    assert fetch.free_quota(make_cfg(tmp_path / 'drive')) > 0
