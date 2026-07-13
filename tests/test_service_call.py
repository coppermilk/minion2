"""CallService: a Step that delegates one file to a web service.

The Telegram <-> service split rides on this Step: it POSTs the job's file
to ``<url>/run-file`` and maps the HTTP outcome to a Verdict. Proved
against a real loopback server (stdlib http.server) so the requests wire
format is genuinely exercised -- 200 -> DELIVERED with the bytes written,
422 -> SKIPPED with the reason, an unreachable port -> FAILED.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from typing import TYPE_CHECKING

from minion_core.adapters.service_call import CallService
from minion_core.adapters.service_call import ServiceCall
from minion_core.kernel import Disposition
from minion_core.kernel import Job
from minion_core.kernel import Origin

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_REPLY: dict[str, object] = {
    'status': 200,
    'body': b'PROCESSED',
    'ctype': 'application/octet-stream',
    'disposition': '',
}
"""The canned reply the loopback handler serves; each test rewrites it."""


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get('Content-Length', '0'))
        self.rfile.read(length)  # drain the multipart upload
        self.send_response(int(_REPLY['status']))  # type: ignore[arg-type]
        self.send_header('Content-Type', str(_REPLY['ctype']))
        disposition = str(_REPLY['disposition'])
        if disposition:
            self.send_header('Content-Disposition', disposition)
        self.end_headers()
        self.wfile.write(bytes(_REPLY['body']))  # type: ignore[arg-type]

    def log_message(self, *_args: object) -> None:
        """Silence the default stderr access log."""


@contextmanager
def _service() -> Iterator[str]:
    srv = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f'http://127.0.0.1:{srv.server_address[1]}'
    finally:
        srv.shutdown()


def _job(tmp_path: Path) -> Job:
    src = tmp_path / 'in.bin'
    src.write_bytes(b'hello')
    dest = tmp_path / 'out'
    dest.mkdir()
    return Job(src=src, dest=dest, stem='in', origin=Origin('tg', '1:2:x'))


def test_delivers_and_writes_the_result(tmp_path: Path) -> None:
    _REPLY.update(
        status=200,
        body=b'PROCESSED',
        ctype='application/octet-stream',
        disposition='',
    )
    with _service() as url:
        verdict = CallService(ServiceCall(url)).process(_job(tmp_path))
    assert verdict.disposition is Disposition.DELIVERED
    assert verdict.result is not None
    assert verdict.result.read_bytes() == b'PROCESSED'


def test_result_is_named_by_content_disposition(tmp_path: Path) -> None:
    # The relay uploads `in.url`, but the service returns a real video name.
    # The bytes must be written under that name, not the `.url` request name,
    # or the sender gets an unplayable file.
    _REPLY.update(
        status=200,
        body=b'VIDEO',
        ctype='video/mp4',
        disposition='attachment; filename="Some Title.mp4"',
    )
    with _service() as url:
        verdict = CallService(ServiceCall(url)).process(_job(tmp_path))
    assert verdict.result is not None
    assert verdict.result.name == 'Some Title.mp4'


def test_422_surfaces_as_skipped(tmp_path: Path) -> None:
    _REPLY.update(
        status=422,
        body=json.dumps({'detail': 'skipped: no_person'}).encode('ascii'),
        ctype='application/json',
        disposition='',
    )
    with _service() as url:
        verdict = CallService(ServiceCall(url)).process(_job(tmp_path))
    assert verdict.disposition is Disposition.SKIPPED
    assert verdict.reason == 'no_person'
    assert verdict.reply  # the sender is told, not left in silence


def test_unreachable_service_is_failed(tmp_path: Path) -> None:
    # Port 1 is never listening -> requests raises -> FAILED.
    verdict = CallService(ServiceCall('http://127.0.0.1:1')).process(
        _job(tmp_path)
    )
    assert verdict.disposition is Disposition.FAILED
    assert verdict.reason == 'service_unreachable'
    assert verdict.reply  # a failure is reported to the sender
