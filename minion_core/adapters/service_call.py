"""Web-service boundary: POST a job's file to an atomic service (requests).

A sanctioned ``requests`` import site (REQ-ARC-002), alongside tg/scripts/
ollama. ``CallService`` is the seam that lets a thin Telegram relay (or any
belt) delegate the transform to a service over HTTP -- bytes up, bytes back
-- instead of loading the model in-process. The IP lives in the service;
this Step only moves bytes, so the relay container stays light.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from dataclasses import replace
from email.message import Message
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

from minion_core import progress
from minion_core.adapters.files import sanitize
from minion_core.kernel import Disposition
from minion_core.kernel import Step
from minion_core.kernel import Verdict
from minion_core.kernel import atomic_write
from minion_core.kernel import next_free_path

if TYPE_CHECKING:
    from collections.abc import Callable

    import requests

    from minion_core.kernel import Job

HTTP_OK = 200
HTTP_UNPROCESSABLE = 422
CALL_TIMEOUT_SEC = 300.0
"""Wall-time bound on one service call (bounded, BLUEPRINT 10)."""

POLL_INTERVAL_SEC = 1.0
"""Seconds between /jobs/{id} polls -- live, but rate-friendly."""

_FALLBACK_NAME = 'result'
"""Output name when the service returns no Content-Disposition (rare)."""

_OFFLINE = 'Sorry, the service is offline right now. Try again shortly.'
_FAILED = 'Sorry, that one failed. Give it another try in a bit.'
_TIMED_OUT = 'Sorry, that took too long. Give it another try.'


@dataclass(frozen=True)
class ServiceCall:
    """Which service to call and how long to wait for it."""

    url: str
    timeout: float = CALL_TIMEOUT_SEC


class CallService(Step):
    """Delegate one file to an atomic web service; write back its result.

    POSTs ``job.src`` to ``<url>/run-file`` and writes the returned bytes
    under ``job.dest``. A 200 is DELIVERED, a 422 is the service's own SKIP
    (its reason surfaced), and anything else -- an unreachable service
    included -- is FAILED. No model is loaded here; the service holds the IP.
    """

    def __init__(self, spec: ServiceCall) -> None:
        self._spec = spec

    def process(self, job: Job) -> Verdict:
        """Call the service; map its HTTP outcome to a Verdict."""
        import requests

        try:
            with job.src.open('rb') as fh:
                resp = requests.post(
                    f'{self._spec.url}/run-file',
                    files={'file': (job.src.name, fh)},
                    timeout=self._spec.timeout,
                )
        except requests.RequestException:
            return Verdict(
                Disposition.FAILED,
                reason='service_unreachable',
                reply='Sorry, the service is offline right now. '
                'Try again shortly.',
            )
        return _verdict(job, resp)


def _verdict(job: Job, resp: requests.Response) -> Verdict:
    """Turn one HTTP response into a Verdict, writing any result bytes.

    A non-200 carries a ``reply``, so the belt's Reply sink tells the sender
    what happened instead of leaving them in silence.
    """
    if resp.status_code == HTTP_OK:
        out = next_free_path(job.dest / _result_name(resp, job.src.name))
        atomic_write(out, resp.content)
        return Verdict(Disposition.DELIVERED, result=out)
    if resp.status_code == HTTP_UNPROCESSABLE:
        reason = _skip_reason(resp)
        return Verdict(
            Disposition.SKIPPED,
            reason=reason,
            reply=f'Nothing to do here ({reason}).',
        )
    return Verdict(
        Disposition.FAILED,
        reason=f'service_http_{resp.status_code}',
        reply='Sorry, that one failed. Give it another try in a bit.',
    )


def _result_name(resp: requests.Response, fallback: str) -> str:
    """The service's own filename for the result, from Content-Disposition.

    The relay uploads a ``.url`` (or spool) file, but the service names the
    result itself: a fetched video is ``Some Title.mp4``, not ``link.url``.
    That name rides back in Content-Disposition. Without it the bytes would
    be written -- and sent -- under the request's name and reach the sender
    with a wrong, unplayable extension. The basename is taken and sanitized:
    the service is a boundary, so its filename is untrusted.
    """
    message = Message()
    message['content-disposition'] = resp.headers.get(
        'content-disposition', ''
    )
    got = message.get_filename()
    if not got:
        return fallback
    return sanitize(Path(got).name)


def _skip_reason(resp: requests.Response) -> str:
    """The service's stable skip code, parsed from its 422 detail."""
    try:
        detail = resp.json().get('detail', '')
    except ValueError:
        return 'service_skip'
    _, _, reason = str(detail).partition(': ')
    return reason or 'service_skip'


class JobClient:
    """Run a file through a service's async /jobs path, streaming progress.

    Submits to ``<url>/jobs/file``, polls ``<url>/jobs/{id}`` feeding each
    percent to ``on_progress``, then fetches the result. Maps the outcome to
    a Verdict just like CallService, so a live-progress belt is a drop-in for
    the synchronous one -- the model still lives in the service.
    """

    def __init__(self, spec: ServiceCall) -> None:
        self._spec = spec

    def run(
        self,
        src: Path,
        dest: Path,
        on_progress: Callable[[progress.Report], None],
    ) -> Verdict:
        """Submit, watch to completion, deliver -- or a spoken failure."""
        import requests

        start = time.monotonic()
        try:
            job_id = self._submit(src)
            done = self._watch(job_id, on_progress)
            verdict = self._collect(job_id, done, dest)
        except requests.RequestException:
            return _failed('service_unreachable', _OFFLINE)
        except TimeoutError:
            return _failed('service_timeout', _TIMED_OUT)
        return _with_detail(verdict, int(time.monotonic() - start))

    def _submit(self, src: Path) -> str:
        import requests

        with src.open('rb') as fh:
            resp = requests.post(
                f'{self._spec.url}/jobs/file',
                files={'file': (src.name, fh)},
                timeout=self._spec.timeout,
            )
        resp.raise_for_status()
        return str(resp.json()['job_id'])

    def _watch(
        self, job_id: str, on_progress: Callable[[progress.Report], None]
    ) -> dict[str, Any]:
        import requests

        deadline = time.monotonic() + self._spec.timeout
        url = f'{self._spec.url}/jobs/{job_id}'
        while time.monotonic() < deadline:
            body = requests.get(url, timeout=self._spec.timeout).json()
            if body.get('status') != 'running':
                return dict(body)
            on_progress(_report_of(body))
            time.sleep(POLL_INTERVAL_SEC)
        raise TimeoutError

    def _collect(
        self, job_id: str, done: dict[str, Any], dest: Path
    ) -> Verdict:
        import requests

        if done.get('status') == 'failed' or done.get('output_ref') is None:
            reason = str(done.get('reason') or 'service_error')
            return Verdict(Disposition.FAILED, reason=reason, reply=_FAILED)
        resp = requests.get(
            f'{self._spec.url}/jobs/{job_id}/result',
            timeout=self._spec.timeout,
        )
        resp.raise_for_status()
        out = next_free_path(dest / _result_name(resp, _FALLBACK_NAME))
        atomic_write(out, resp.content)
        return Verdict(Disposition.DELIVERED, result=out)


def _report_of(body: dict[str, Any]) -> progress.Report:
    """A live job-status body as a progress Report (percent, bytes, ETA)."""
    return progress.Report(
        int(body.get('progress', 0)),
        int(body.get('done_bytes', 0)),
        int(body.get('total_bytes', 0)),
        int(body.get('eta_sec', 0)),
    )


def _failed(reason: str, reply: str) -> Verdict:
    """A spoken failure verdict (the sender is always told what went wrong)."""
    return Verdict(Disposition.FAILED, reason=reason, reply=reply)


def _with_detail(verdict: Verdict, elapsed_sec: int) -> Verdict:
    """Stamp a delivered verdict's reply with the done detail (size . time).

    The reply carries the final ``47.6 MB . 47s`` line for the status sink to
    show on the 'done' step; a non-delivery is returned untouched.
    """
    if verdict.disposition is not Disposition.DELIVERED:
        return verdict
    if verdict.result is None:
        return verdict
    size = verdict.result.stat().st_size
    return replace(verdict, reply=progress.done_detail(size, elapsed_sec))
