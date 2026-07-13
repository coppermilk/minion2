"""Web-service boundary: POST a job's file to an atomic service (requests).

A sanctioned ``requests`` import site (REQ-ARC-002), alongside tg/scripts/
ollama. ``CallService`` is the seam that lets a thin Telegram relay (or any
belt) delegate the transform to a service over HTTP -- bytes up, bytes back
-- instead of loading the model in-process. The IP lives in the service;
this Step only moves bytes, so the relay container stays light.
"""

from __future__ import annotations

from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import TYPE_CHECKING

from minion_core.adapters.files import sanitize
from minion_core.kernel import Disposition
from minion_core.kernel import Step
from minion_core.kernel import Verdict
from minion_core.kernel import atomic_write
from minion_core.kernel import next_free_path

if TYPE_CHECKING:
    import requests

    from minion_core.kernel import Job

HTTP_OK = 200
HTTP_UNPROCESSABLE = 422
CALL_TIMEOUT_SEC = 300.0
"""Wall-time bound on one service call (bounded, BLUEPRINT 10)."""


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
