"""Link download boundary: yt-dlp + timeout + quota + SSRF guard.

Owns the pinned ``yt-dlp`` binary (OPERATIONS 3); every download is
wall-time bounded (REQ-RES-001), disk-bounded on both sides
(REQ-RES-002) and host-bounded pre-connect (REQ-SEC-001).

Invariants deliberately not knobs: one link -> one file
(``--no-playlist``); the output name is never guessed
(``--print after_move:filepath``).
"""

from __future__ import annotations

import re
import socket
import subprocess
import threading
import urllib.request
from collections import deque
from ipaddress import IPv4Address
from ipaddress import IPv6Address
from ipaddress import ip_address
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING
from typing import Protocol
from urllib.parse import urlsplit

from minion_core import progress
from minion_core.adapters.files import BudgetWriter
from minion_core.adapters.files import QuotaExceeded
from minion_core.adapters.files import free_quota
from minion_core.adapters.files import sanitize
from minion_core.kernel import Disposition
from minion_core.kernel import Step
from minion_core.kernel import Verdict

if TYPE_CHECKING:
    from collections.abc import Callable
    from http.client import HTTPMessage
    from typing import IO

    from minion_core.kernel import Job
    from minion_core.settings import Settings

YTDLP = 'yt-dlp'
"""The pinned extractor binary (a managed artifact, BLUEPRINT 12)."""

CHUNK = 64 * 1024
"""Direct-transfer streaming chunk size."""

_YOUTUBE_HOSTS = (
    'youtube.com',
    'www.youtube.com',
    'youtu.be',
    'm.youtube.com',
    'music.youtube.com',
)


class Blocked(Exception):
    """SSRF guard rejection; reason code ``ssrf_blocked``."""


class FetchFailed(Exception):
    """The extractor gave up; reason code ``stale_extractor``."""


def guard(url: str) -> None:
    """Reject private/reserved destinations pre-connect (REQ-SEC-001).

    The chat allow-list remains the primary control; this guard is
    defence in depth (OPERATIONS 3). Do not whitelist.
    """
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https'):
        raise Blocked(f'ssrf_blocked: scheme {parts.scheme!r}')
    host = parts.hostname
    if not host:
        raise Blocked('ssrf_blocked: no host')
    for addr in _addresses(host):
        if not addr.is_global:
            raise Blocked(f'ssrf_blocked: {host} -> {addr}')


def _addresses(host: str) -> list[IPv4Address | IPv6Address]:
    """Every address the host resolves to; unresolvable -> Blocked."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise Blocked(f'ssrf_blocked: unresolvable {host}') from exc
    return [ip_address(str(info[4][0])) for info in infos]


def download(url: str, into: Path, cfg: Settings) -> Path:
    """One link -> one file via yt-dlp, fully bounded, live progress.

    Streams yt-dlp's percent to the progress sink (minion_core.progress)
    while it runs, so a caller (svc-fetch's job) can show a live bar.
    Raises Blocked / QuotaExceeded / subprocess.TimeoutExpired /
    FetchFailed; the FetchLink step maps each to its reason code.
    """
    guard(url)
    if free_quota(cfg) <= 0:
        raise QuotaExceeded('quota_exceeded: pre-transfer')
    into.mkdir(parents=True, exist_ok=True)
    return _run_ytdlp(_argv(url, into, cfg), cfg.download_timeout_sec)


_PCT = re.compile(r'PROGRESS:\s*([0-9.]+)%')
"""The percent from a --progress-template line (our PROGRESS: prefix)."""


class _Pump:
    """Reads one yt-dlp stream live: percents to the sink, the rest kept.

    Both stdout and stderr are pumped, because a build may emit the
    ``--progress-template`` line on either -- reading both means the bar
    animates whichever stream carries it. The kept non-progress lines are
    the error tail (stderr) or the ``--print`` filepath (stdout).
    """

    def __init__(self, report: Callable[[int], None]) -> None:
        self._report = report
        self.tail: deque[str] = deque(maxlen=20)

    def run(self, stream: IO[str]) -> None:
        """Drain the stream until EOF (the process closing it)."""
        for line in stream:
            match = _PCT.search(line)
            if match is not None:
                self._report(int(float(match.group(1))))
            else:
                kept = line.rstrip()
                if kept:
                    self.tail.append(kept)


def _run_ytdlp(argv: list[str], timeout: float) -> Path:
    """Run yt-dlp bounded; stream progress; return the output path.

    The total wall-time bound is ``proc.wait(timeout)`` (survives a silent
    hang, REQ-RES-001); a reader thread per pipe drains both, so neither can
    deadlock and progress is caught whichever stream yt-dlp writes it to.
    """
    report = progress.current() or _ignore
    out_pump, err_pump = _Pump(report), _Pump(report)
    proc = subprocess.Popen(  # noqa: S603 -- fixed binary, no shell
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    readers = [
        threading.Thread(
            target=out_pump.run, args=(proc.stdout,), daemon=True
        ),
        threading.Thread(
            target=err_pump.run, args=(proc.stderr,), daemon=True
        ),
    ]
    for reader in readers:
        reader.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    finally:
        for reader in readers:
            reader.join(timeout=1.0)
    if proc.returncode != 0:
        tail = ' | '.join(err_pump.tail) or ' | '.join(out_pump.tail)
        raise FetchFailed(f'stale_extractor: {tail[-500:]}')
    return _output_path(out_pump.tail)


def _ignore(_pct: int) -> None:
    """The sink when nobody is listening (a bare download)."""


def _output_path(lines: deque[str]) -> Path:
    """The last stdout line (the --print filepath)."""
    got = Path(lines[-1]) if lines else Path()
    if not got.is_file():
        raise FetchFailed('stale_extractor: no output file')
    return got


def _argv(url: str, into: Path, cfg: Settings) -> list[str]:
    """The yt-dlp invocation; volatile knobs are Settings."""
    argv = [
        YTDLP,
        '--no-playlist',
        '--no-simulate',
        # Force progress even without a TTY (a container has none), on its
        # own lines, in our parseable form -- else yt-dlp prints no percent
        # and the live bar never moves.
        '--progress',
        '--newline',
        '--progress-template',
        'PROGRESS:%(progress._percent_str)s',
        '--print',
        'after_move:filepath',
        '-f',
        cfg.ytdlp_format,
        '--merge-output-format',
        cfg.ytdlp_container,
        '-P',
        str(into),
        '-o',
        '%(title).80s.%(ext)s',
    ]
    if _is_youtube(url):
        clients = ','.join(cfg.ytdlp_player_clients)
        argv += ['--extractor-args', f'youtube:player_client={clients}']
    return [*argv, url]


def _is_youtube(url: str) -> bool:
    """Whether the player_client extractor-arg applies."""
    host = urlsplit(url).hostname or ''
    return host.lower() in _YOUTUBE_HOSTS


def download_direct(url: str, target: Path, cfg: Settings) -> Path:
    """Stream a direct transfer with mid-stream bounds (REQ-RES-002).

    Redirect hops re-enter the SSRF guard, so a public host cannot
    bounce the fetch into a private one.
    """
    guard(url)
    budget = free_quota(cfg)
    if budget <= 0:
        raise QuotaExceeded('quota_exceeded: pre-transfer')
    deadline = monotonic() + cfg.download_timeout_sec
    opener = urllib.request.build_opener(_GuardedRedirect())
    writer = BudgetWriter(target, budget)
    try:
        with opener.open(url, timeout=cfg.download_timeout_sec) as resp:
            _pull(resp, writer, deadline)
    except BaseException:
        writer.abort()
        raise
    return writer.commit()


class _Readable(Protocol):
    """The slice of a response object the puller needs."""

    def read(self, n: int) -> bytes:
        """Return up to ``n`` bytes; empty means done."""


def _pull(resp: _Readable, writer: BudgetWriter, deadline: float) -> None:
    """Copy chunks under the wall-time bound (REQ-RES-001)."""
    while True:
        if monotonic() > deadline:
            raise TimeoutError('download_timeout: direct transfer')
        chunk = resp.read(CHUNK)
        if not chunk:
            return
        writer.write(chunk)


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """Re-guard every redirect hop (SSRF defence in depth)."""

    def redirect_request(  # noqa: PLR0913 -- stdlib hook signature
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        """Check the new destination before following it."""
        guard(newurl)
        return super().redirect_request(
            req,
            fp,
            code,
            msg,
            headers,
            newurl,
        )


_FAILURES: tuple[tuple[type[BaseException], Disposition, str], ...] = (
    (Blocked, Disposition.REJECTED, 'ssrf_blocked'),
    (QuotaExceeded, Disposition.REJECTED, 'quota_exceeded'),
    (subprocess.TimeoutExpired, Disposition.FAILED, 'download_timeout'),
    (TimeoutError, Disposition.FAILED, 'download_timeout'),
    (FetchFailed, Disposition.FAILED, 'stale_extractor'),
)

_CAUGHT = (
    Blocked,
    QuotaExceeded,
    subprocess.TimeoutExpired,
    TimeoutError,
    FetchFailed,
)


class FetchLink(Step):
    """Download the link stored in a ``.url`` spool file.

    Non-link inputs pass through delivered, so the step composes
    into mixed link/media belts (frames).
    """

    def __init__(self, cfg: Settings) -> None:
        self._cfg = cfg

    def process(self, job: Job) -> Verdict:
        """Fetch the link; map each failure to its reason code."""
        if job.src.suffix != '.url':
            return Verdict(Disposition.DELIVERED, result=job.src)
        url = job.src.read_text(encoding='ascii').strip()
        try:
            got = download(url, job.dest, self._cfg)
        except _CAUGHT as exc:
            return _failure(exc)
        return Verdict(
            Disposition.DELIVERED,
            result=got,
            reply=f'fetched {sanitize(got.name)}',
        )


def _failure(exc: BaseException) -> Verdict:
    """The stable reason code for a bounded fetch failure."""
    for kind, disposition, reason in _FAILURES:
        if isinstance(exc, kind):
            return Verdict(disposition, reason=reason)
    return Verdict(Disposition.FAILED, reason='step_crashed')
