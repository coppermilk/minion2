"""HTTP/OpenAPI skin over the service core (PLATFORM.md, section 3).

One generic app, parameterized by the STEP env var, so N services are N
containers of one image. FastAPI serves /openapi.json for free -- what
n8n's HTTP Request node and our orchestrator consume. /healthz is the
container probe. No host port in offline mode: the compose network only.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from fastapi import FastAPI
from pydantic import BaseModel

from services.core import ServiceRequest
from services.core import run_service
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

    return app


app = create_app(os.environ.get('STEP', 'deliver'), store_from_env())
"""The module-level app uvicorn serves; STEP/STORE_* pick behaviour."""
