"""HTTP/OpenAPI skin over the service core.

One generic app, parameterized by the STEP env var, so N services are N
containers of one image. FastAPI serves /openapi.json for free -- what
n8n's HTTP Request node and any web consumer read. /healthz is the
container probe.

Ways in (bytes in, bytes out -- no shared object store):
- ``/run-file`` takes an uploaded file and returns the result file. The
  frictionless path for n8n's HTTP Request node, a thin relay, or any
  caller with the media as binary. Synchronous: fine up to ~a minute.
- ``/jobs/file`` is the async path for slow Steps: submit returns 202 + a
  job id at once, the Step runs in the background, and the caller learns it
  is ready by polling ``/jobs/{id}`` or via a webhook ``callback_url`` -- so
  no connection is held for a minute. The upload and result live in a
  per-process temp store until fetched.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import threading
import uuid
import zipfile
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from services.core import ServiceRequest
from services.core import ServiceResult
from services.core import run_service
from services.store import LocalStore

if TYPE_CHECKING:
    from services.store import Store


@dataclass
class _Job:
    """One async job's live state (in-memory, per service process)."""

    id: str
    status: str  # running | done | failed
    disposition: str = ''
    reason: str = ''
    ms: float = 0.0
    output_ref: str | None = None
    outputs: list[str] = field(default_factory=list)
    error: str = ''


class JobStore:
    """Thread-safe registry of a service's async jobs."""

    def __init__(self) -> None:
        self._jobs: dict[str, _Job] = {}
        self._lock = threading.Lock()

    def create(self) -> str:
        """Register a new running job; return its id."""
        job_id = uuid.uuid4().hex
        with self._lock:
            self._jobs[job_id] = _Job(id=job_id, status='running')
        return job_id

    def get(self, job_id: str) -> _Job | None:
        """The job by id, or None."""
        with self._lock:
            return self._jobs.get(job_id)

    def finish(self, job_id: str, result: ServiceResult) -> None:
        """Record a completed run's outcome on the job."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = 'done'
            job.disposition = result.disposition
            job.reason = result.reason
            job.ms = result.ms
            job.output_ref = result.output_ref
            job.outputs = result.outputs

    def fail(self, job_id: str, error: str) -> None:
        """Mark a job failed with an error message."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.status = 'failed'
                job.error = error


@dataclass(frozen=True)
class _JobSpec:
    """What a background job needs to run and where to call back."""

    step: str
    store: Store
    ref: str
    callback: str | None


def _summary(job: _Job) -> dict[str, object]:
    """The public view of a job (status endpoint and webhook body)."""
    return {
        'job_id': job.id,
        'status': job.status,
        'disposition': job.disposition,
        'reason': job.reason,
        'ms': job.ms,
        'output_ref': job.output_ref,
        'outputs': job.outputs,
        'error': job.error,
    }


def _callback(url: str, job: _Job) -> None:
    """POST the finished job to a webhook (best-effort)."""
    import httpx

    with contextlib.suppress(httpx.HTTPError):
        httpx.post(url, json=_summary(job), timeout=10.0)


def _run_job(jobs: JobStore, job_id: str, spec: _JobSpec) -> None:
    """Run one job in the background, then fire its callback if any."""
    try:
        result = run_service(ServiceRequest(spec.step, spec.ref), spec.store)
        jobs.finish(job_id, result)
    except Exception as exc:  # noqa: BLE001 -- a job failure is reported, not raised
        jobs.fail(job_id, str(exc))
    done = jobs.get(job_id)
    if spec.callback and done is not None:
        _callback(spec.callback, done)


def _stash_to(store: Store, job_id: str, file: UploadFile) -> str:
    """Save an upload into the store under the job's prefix; return its ref."""
    work = Path(tempfile.mkdtemp())
    name = file.filename or 'input'
    src = work / name
    with src.open('wb') as sink:
        shutil.copyfileobj(file.file, sink)
    ref = store.put(f'jobs-in/{job_id}/{name}', src)
    shutil.rmtree(work, ignore_errors=True)
    return ref


def _spawn(jobs: JobStore, job_id: str, spec: _JobSpec) -> None:
    """Start a job's background thread (daemon)."""
    threading.Thread(
        target=_run_job, args=(jobs, job_id, spec), daemon=True
    ).start()


def create_app(step: str) -> FastAPI:
    """Build the one-Step service app (HTTP + OpenAPI)."""
    app = FastAPI(title=f'service:{step}')
    jobs = JobStore()
    job_store: Store = LocalStore(Path(tempfile.mkdtemp(prefix='svc-jobs-')))

    @app.get('/healthz')
    def healthz() -> dict[str, str]:
        return {'status': 'ok', 'step': step}

    @app.post('/run-file')
    def run_file(file: UploadFile) -> FileResponse:
        work = Path(tempfile.mkdtemp())
        cleanup = BackgroundTask(shutil.rmtree, work, ignore_errors=True)
        ref = _stash(work, file)
        result = run_service(ServiceRequest(step, ref), store_at(work))
        headers = {
            'X-Disposition': result.disposition,
            'X-Run-Ms': f'{result.ms:.3f}',
        }
        if result.output_ref is None and result.outputs:
            zpath = _zip_of(work, result.outputs)  # a folder result (frames)
            return FileResponse(
                zpath,
                filename=zpath.name,
                media_type='application/zip',
                background=cleanup,
                headers=headers,
            )
        if result.output_ref is None:
            shutil.rmtree(work, ignore_errors=True)
            raise HTTPException(
                status_code=422,
                detail=f'{result.disposition}: {result.reason}',
            )
        out = store_at(work).fetch(result.output_ref, work / 'out')
        return FileResponse(
            out, filename=out.name, background=cleanup, headers=headers
        )

    @app.post('/jobs/file', status_code=202)
    def submit_file(
        file: UploadFile, callback_url: str | None = None
    ) -> dict[str, str]:
        job_id = jobs.create()
        ref = _stash_to(job_store, job_id, file)
        _spawn(jobs, job_id, _JobSpec(step, job_store, ref, callback_url))
        return {'job_id': job_id, 'status': 'running'}

    @app.get('/jobs/{job_id}')
    def job_status(job_id: str) -> dict[str, object]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail='job not found')
        return _summary(job)

    @app.get('/jobs/{job_id}/result')
    def job_result(job_id: str) -> FileResponse:
        job = jobs.get(job_id)
        if job is None or job.status != 'done':
            raise HTTPException(status_code=409, detail='job not done')
        if job.output_ref is None:
            raise HTTPException(
                status_code=422, detail=f'{job.disposition}: {job.reason}'
            )
        work = Path(tempfile.mkdtemp())
        out = job_store.fetch(job.output_ref, work / 'out')
        return FileResponse(
            out,
            filename=out.name,
            background=BackgroundTask(shutil.rmtree, work, ignore_errors=True),
        )

    return app


def _zip_of(work: Path, refs: list[str]) -> Path:
    """Zip a directory result's files (e.g. frames) into one archive."""
    zpath = work / 'result.zip'
    with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED) as archive:
        for ref in refs:
            path = Path(urlparse(ref).path)
            archive.write(path, arcname=path.name)
    return zpath


def store_at(work: Path) -> LocalStore:
    """An ephemeral LocalStore for one bytes-in/bytes-out request."""
    return LocalStore(work / 'store')


def _stash(work: Path, file: UploadFile) -> str:
    """Save the upload under the request store; return its ref."""
    name = file.filename or 'input'
    src = work / 'in' / name
    src.parent.mkdir(parents=True, exist_ok=True)
    with src.open('wb') as sink:
        shutil.copyfileobj(file.file, sink)
    return store_at(work).put(f'in/{name}', src)


app = create_app(os.environ.get('STEP', 'deliver'))
"""The module-level app uvicorn serves; STEP picks the Step."""
