"""HTTP/OpenAPI skin over the service core (PLATFORM.md, section 3).

One generic app, parameterized by the STEP env var, so N services are N
containers of one image. FastAPI serves /openapi.json for free -- what
n8n's HTTP Request node and our orchestrator consume. /healthz is the
container probe. No host port in offline mode: the compose network only.

Two ways in:
- ``/run`` takes an object-store ref and returns a ref (the orchestrator
  and Mode B data plane).
- ``/run-file`` takes an uploaded file and returns the result file (bytes
  in, bytes out) -- the frictionless path for n8n's HTTP Request node,
  which has the media as binary and wants binary back, no S3 node needed.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from services.core import ServiceRequest
from services.core import run_service
from services.store import LocalStore
from services.store import store_from_env

if TYPE_CHECKING:
    from services.store import Store


class RunBody(BaseModel):
    """The /run request body: the input object's ref."""

    input_ref: str


class RunReply(BaseModel):
    """The /run response: output ref(s), verdict, and Step timing."""

    output_ref: str | None
    outputs: list[str]
    disposition: str
    reason: str
    ms: float


def create_app(step: str, store: Store) -> FastAPI:
    """Build the one-Step service app (HTTP + OpenAPI)."""
    app = FastAPI(title=f'service:{step}')

    @app.get('/healthz')
    def healthz() -> dict[str, str]:
        return {'status': 'ok', 'step': step}

    @app.post('/run')
    def run(body: RunBody) -> RunReply:
        result = run_service(ServiceRequest(step, body.input_ref), store)
        return RunReply(
            output_ref=result.output_ref,
            outputs=result.outputs,
            disposition=result.disposition,
            reason=result.reason,
            ms=result.ms,
        )

    @app.post('/run-file')
    def run_file(file: UploadFile) -> FileResponse:
        work = Path(tempfile.mkdtemp())
        cleanup = BackgroundTask(shutil.rmtree, work, ignore_errors=True)
        ref = _stash(work, file)
        result = run_service(ServiceRequest(step, ref), store_at(work))
        if result.output_ref is None:
            shutil.rmtree(work, ignore_errors=True)
            raise HTTPException(
                status_code=422,
                detail=f'{result.disposition}: {result.reason}',
            )
        out = store_at(work).fetch(result.output_ref, work / 'out')
        return FileResponse(
            out,
            filename=out.name,
            background=cleanup,
            headers={
                'X-Disposition': result.disposition,
                'X-Run-Ms': f'{result.ms:.3f}',
            },
        )

    return app


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


app = create_app(os.environ.get('STEP', 'deliver'), store_from_env())
"""The module-level app uvicorn serves; STEP/STORE_* pick behaviour."""
