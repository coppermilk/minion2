"""Async jobs on a service: submit -> poll / callback (slow Steps).

Services tier. A slow Step should not hold the HTTP connection: /jobs/file
returns 202 + a job id at once, the Step runs in a background thread, and
the caller learns it is ready by polling /jobs/{id} or via a webhook
callback. Uses the fast `deliver` Step; timing is the only thing that
differs for a real minute-long Step.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from minion_core.adapters.files import Deliver
from services.http import create_app


def _client() -> TestClient:
    return TestClient(create_app('deliver', lambda _c: Deliver()))


def _wait_done(client: TestClient, job_id: str) -> dict:
    for _ in range(200):
        body = client.get(f'/jobs/{job_id}').json()
        if body['status'] != 'running':
            return body
        time.sleep(0.02)
    msg = 'job never left running'
    raise AssertionError(msg)


def test_async_file_job() -> None:
    client = _client()
    submit = client.post(
        '/jobs/file',
        files={'file': ('a.bin', b'bytes', 'application/octet-stream')},
    )
    assert submit.status_code == 202
    job_id = submit.json()['job_id']

    done = _wait_done(client, job_id)
    assert done['status'] == 'done'
    assert client.get(f'/jobs/{job_id}/result').content == b'bytes'


def test_job_reports_live_progress() -> None:
    """A Step's live percent (via the sink) is visible on /jobs/{id}."""
    from minion_core import progress
    from minion_core.kernel import Disposition
    from minion_core.kernel import Step
    from minion_core.kernel import Verdict

    class _Prog(Step):
        def process(self, job):
            sink = progress.current()
            if sink is not None:
                sink(42)
            return Verdict(Disposition.DELIVERED, result=job.src)

    client = TestClient(create_app('prog', lambda _c: _Prog()))
    submit = client.post(
        '/jobs/file',
        files={'file': ('a.bin', b'x', 'application/octet-stream')},
    )
    done = _wait_done(client, submit.json()['job_id'])
    assert done['status'] == 'done'
    assert done['progress'] == 42  # the reported percent survived


def test_unknown_job_is_404() -> None:
    client = _client()
    assert client.get('/jobs/nope').status_code == 404
    assert client.get('/jobs/nope/result').status_code == 409


def test_job_fires_webhook_callback(monkeypatch) -> None:
    import httpx

    calls: list[tuple[str, dict]] = []

    def fake_post(url: str, json: dict, timeout: float) -> object:
        calls.append((url, json))
        return object()

    monkeypatch.setattr(httpx, 'post', fake_post)
    client = _client()

    submit = client.post(
        '/jobs/file',
        params={'callback_url': 'http://cb.example/hook'},
        files={'file': ('a.bin', b'x', 'application/octet-stream')},
    )
    job_id = submit.json()['job_id']
    _wait_done(client, job_id)
    for _ in range(100):
        if calls:
            break
        time.sleep(0.02)
    assert calls
    assert calls[0][0] == 'http://cb.example/hook'
    assert calls[0][1]['status'] == 'done'
