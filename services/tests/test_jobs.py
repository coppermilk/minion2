"""Async jobs on a service: submit -> poll / callback (slow Steps).

Services tier. A slow Step should not hold the HTTP connection: /jobs (or
/jobs/file) returns 202 + a job id at once, the Step runs in a background
thread, and the caller learns it is ready by polling /jobs/{id} or via a
webhook callback. Uses the fast `deliver` Step; timing is the only thing
that differs for a real minute-long Step.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from services.http import create_app
from services.store import LocalStore

if TYPE_CHECKING:
    from pathlib import Path


def _setup(tmp_path: Path):
    store = LocalStore(tmp_path / 'store')
    return TestClient(create_app('deliver', store)), store


def _wait_done(client: TestClient, job_id: str) -> dict:
    for _ in range(200):
        body = client.get(f'/jobs/{job_id}').json()
        if body['status'] != 'running':
            return body
        time.sleep(0.02)
    msg = 'job never left running'
    raise AssertionError(msg)


def test_async_ref_job(tmp_path: Path) -> None:
    client, store = _setup(tmp_path)
    src = tmp_path / 'in.bin'
    src.write_bytes(b'hello')
    ref = store.put('inbox/in.bin', src)

    submit = client.post('/jobs', json={'input_ref': ref})
    assert submit.status_code == 202
    job_id = submit.json()['job_id']

    done = _wait_done(client, job_id)
    assert done['status'] == 'done'
    assert done['disposition'] == 'delivered'

    result = client.get(f'/jobs/{job_id}/result')
    assert result.status_code == 200
    assert result.content == b'hello'


def test_async_file_job(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    submit = client.post(
        '/jobs/file',
        files={'file': ('a.bin', b'bytes', 'application/octet-stream')},
    )
    assert submit.status_code == 202
    job_id = submit.json()['job_id']

    done = _wait_done(client, job_id)
    assert done['status'] == 'done'
    assert client.get(f'/jobs/{job_id}/result').content == b'bytes'


def test_unknown_job_is_404(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    assert client.get('/jobs/nope').status_code == 404
    assert client.get('/jobs/nope/result').status_code == 409


def test_job_fires_webhook_callback(tmp_path: Path, monkeypatch) -> None:
    import httpx

    calls: list[tuple[str, dict]] = []

    def fake_post(url: str, json: dict, timeout: float) -> object:
        calls.append((url, json))
        return object()

    monkeypatch.setattr(httpx, 'post', fake_post)
    client, store = _setup(tmp_path)
    src = tmp_path / 'in.bin'
    src.write_bytes(b'x')
    ref = store.put('inbox/in.bin', src)

    submit = client.post(
        '/jobs',
        params={'callback_url': 'http://cb.example/hook'},
        json={'input_ref': ref},
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
