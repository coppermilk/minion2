"""relay bot: a thin Telegram transport in front of an atomic service.

Graph: (TgMedia | Folder) -> CallService -> RouteOrigin(TgStatus / nothing)
-> Shelve. TgStatus drives one self-editing Telegram message through the
job's life (sending -> done, or a plain error), so the sender never gets a
pile of messages and is always told the outcome. The heavy transform
(blur, frames, ...) lives in a separate service container; this container
only receives a document over Telegram (or a folder drop), POSTs it to
``SERVICE_URL/run-file``, and sends the bytes back -- no model, no torch. N
containers: ``SERVICE_URL`` picks the service and ``RELAY_NAME`` the work
dir / offset, so ``tg-censor-blur`` and ``tg-frames`` are the same image
with different env (the Telegram <-> service split).
"""

from __future__ import annotations

import functools
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING

from minion_core.adapters.files import Shelve
from minion_core.adapters.files import free_quota
from minion_core.adapters.service_call import CallService
from minion_core.adapters.service_call import JobClient
from minion_core.adapters.service_call import ServiceCall
from minion_core.adapters.tg import SpoolSpec
from minion_core.adapters.tg import TgAny
from minion_core.adapters.tg import TgApi
from minion_core.adapters.tg import TgChannel
from minion_core.adapters.tg import TgError
from minion_core.adapters.tg import TgLinks
from minion_core.adapters.tg import TgMedia
from minion_core.adapters.tg import TgSpec
from minion_core.adapters.tg import chats_from
from minion_core.adapters.tg import spooled_or_dropped
from minion_core.kernel import Disposition
from minion_core.kernel import Envelope
from minion_core.kernel import FolderSpec
from minion_core.kernel import Null
from minion_core.kernel import RouteOrigin
from minion_core.kernel import SeenPaths
from minion_core.kernel import Sink
from minion_core.kernel import Step
from minion_core.kernel import merge_watch
from minion_core.kernel import run
from minion_core.progress import Report
from minion_core.settings import load
from minions.telegram.progress_style import DONE
from minions.telegram.progress_style import DOWNLOADING
from minions.telegram.progress_style import ERROR
from minions.telegram.progress_style import RECEIVED
from minions.telegram.progress_style import SENDING
from minions.telegram.progress_style import Style
from minions.telegram.progress_style import style_for

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from minion_core.kernel import Job
    from minion_core.kernel import Origin
    from minion_core.kernel import Source
    from minion_core.kernel import Stage
    from minion_core.kernel import Verdict
    from minion_core.settings import Settings

_DOCKS = {'media': TgMedia, 'any': TgAny, 'links': TgLinks}
"""RELAY_DOCK -> the Telegram dock: documents, links+documents, or links."""

_DEFAULT_EXTS = (
    '.jpg',
    '.jpeg',
    '.png',
    '.webp',
    '.mp4',
    '.mkv',
    '.webm',
    '.mov',
    '.avi',
)
"""Media suffixes the folder drop accepts by default (RELAY_EXTS overrides)."""

_DEFAULT_HELP = 'Send or drop a file and I run it through the service.'

_ACKS = {
    'censor-blur': 'Got it -- blurring the people in your photo...',
    'censor-black': 'Got it -- blacking out the faces...',
    'restore': 'Got it -- erasing the people and repainting the scene...',
    'frames': 'Got it -- extracting the frames, back in a moment...',
    'fetch': 'Got it -- fetching the video...',
    'fan-save': 'Got it -- saving the video to your queue...',
}
"""Per-bot acknowledgement, sent the moment a message is seen (before the
download). Keyed by RELAY_NAME; an unlisted bot falls back to a generic ack."""

_DEFAULT_ACK = 'Got it -- working on it...'

_DEFAULT_FAIL = 'Sorry, that did not work. Give it another try in a bit.'

_SEND_FAIL = (
    'Downloaded, but could not send it -- the file may be over the '
    'Telegram 50 MB limit for bots.'
)


class TgStatus(Sink):
    """Drive one self-editing Telegram message through a job's life.

    Replaces SendResult+Reply on the tg side: it edits ONE message as a
    growing checklist -- sending, then done -- and, crucially, tells the
    sender when the UPLOAD itself fails (a >50 MB video), instead of
    leaving the message stuck on 'Sending...'. Every path is terminal:
    the sender is always told, in the same message, never left hanging.
    """

    def __init__(self, channel: TgChannel, style: Style) -> None:
        self._channel = channel
        self._style = style

    def handle(self, env: Envelope) -> None:
        """Edit the status toward the outcome (delivered file or error)."""
        verdict = env.verdict
        if verdict is None:
            return
        origin = env.job.origin
        result = verdict.result
        if verdict.disposition is Disposition.DELIVERED and result is not None:
            self._deliver(origin, result, verdict.reply)
            return
        self._edit(origin, ERROR, verdict.reply or _DEFAULT_FAIL)

    def _deliver(self, origin: Origin, result: Path, detail: str) -> None:
        """Announce sending, upload the file, then mark done -- or report.

        A failed upload (over the 50 MB bot limit, a timeout) is caught and
        shown, so the message never freezes on 'sending'. ``detail`` is the
        done line (size and elapsed) the caller measured.
        """
        self._edit(origin, SENDING, '')
        try:
            self._channel.send_file(origin, result)
        except TgError:
            self._edit(origin, ERROR, _SEND_FAIL)
            return
        self._edit(origin, DONE, detail)

    def _edit(self, origin: Origin, phase: str, detail: str) -> None:
        """Render one status block for the phase and edit the message."""
        self._channel.edit_text(
            origin, self._style.render(phase, Report(100), detail)
        )


_PROGRESS_STEP = 5
"""Only redraw the bar when the percent crosses a 5% boundary."""

_MIN_EDIT_SEC = 3.0
"""...and at most this often, to stay under Telegram's edit rate limit."""


class _Throttle:
    """An adequate edit cadence: a new STEP bucket, and MIN_EDIT_SEC apart."""

    def __init__(self, step: int, min_sec: float) -> None:
        self._step = step
        self._min_sec = min_sec
        self._shown = -1
        self._last = 0.0

    def due(self, pct: int) -> bool:
        """Whether the bar should be redrawn for this percent, now.

        The first draw always fires (nothing shown yet); later draws need
        both a new bucket and the minimum interval since the last one.
        """
        bucket = pct // self._step
        if bucket <= self._shown:
            return False
        now = monotonic()
        if self._shown >= 0 and now - self._last < self._min_sec:
            return False
        self._shown = bucket
        self._last = now
        return True


class CallServiceLive(Step):
    """Delegate to a service's async /jobs path, editing a live progress bar.

    For slow downloads (the links dock): submit, poll, and edit the ack with
    the styled percent at an adequate step, then return the Verdict (TgStatus
    does sending/done/error). The model still lives in the service.
    """

    def __init__(
        self, spec: ServiceCall, channel: TgChannel, style: Style
    ) -> None:
        self._client = JobClient(spec)
        self._channel = channel
        self._style = style

    def process(self, job: Job) -> Verdict:
        """Run the job, streaming a throttled progress bar to the ack."""
        throttle = _Throttle(_PROGRESS_STEP, _MIN_EDIT_SEC)
        origin = job.origin
        self._channel.edit_text(
            origin, self._style.render(RECEIVED, Report(0))
        )

        def on_progress(report: Report) -> None:
            if throttle.due(report.pct):
                text = self._style.render(DOWNLOADING, report)
                self._channel.edit_text(origin, text)

        return self._client.run(job.src, job.dest, on_progress)


def _name(env: Mapping[str, str]) -> str:
    """The relay's identity: its work dir, offset, and done folder."""
    return env.get('RELAY_NAME', 'relay')


def _exts(env: Mapping[str, str]) -> tuple[str, ...]:
    """The watched suffixes: RELAY_EXTS (csv) or the media default."""
    raw = env.get('RELAY_EXTS', '')
    if not raw.strip():
        return _DEFAULT_EXTS
    parts = (p.strip().lower() for p in raw.split(','))
    return tuple(p if p.startswith('.') else f'.{p}' for p in parts if p)


def _dock(env: Mapping[str, str], api: TgApi, spec: TgSpec) -> Source:
    """The Telegram dock chosen by RELAY_DOCK (media | any | links)."""
    make = _DOCKS.get(env.get('RELAY_DOCK', 'media'), TgMedia)
    return make(api, spec)


_LINKS_TIMEOUT_SEC = 1800.0
"""How long the relay waits on a download job -- longer than the service's
own download bound (DOWNLOAD_TIMEOUT_SEC, 900s), so a slow but healthy
download is never dropped as 'lost' before the service finishes it."""


def _call(env: Mapping[str, str], channel: TgChannel, style: Style) -> Step:
    """The service caller for this dock's speed.

    A live progress bar for links (slow downloads), with a generous wait so
    a long download is not dropped; the plain synchronous call otherwise
    (fast pixel services need no bar).
    """
    url = env.get('SERVICE_URL', '')
    if env.get('RELAY_DOCK', 'media') == 'links':
        spec = ServiceCall(url, _LINKS_TIMEOUT_SEC)
        return CallServiceLive(spec, channel, style)
    return CallService(ServiceCall(url))


_WORKERS_DEFAULT = 4
"""Concurrent downloads on a links belt (bounded; many links, many users)."""


def _workers(env: Mapping[str, str]) -> int:
    """RELAY_WORKERS (>=1), or the default concurrency."""
    try:
        return max(1, int(env.get('RELAY_WORKERS', str(_WORKERS_DEFAULT))))
    except ValueError:
        return _WORKERS_DEFAULT


@dataclass(frozen=True)
class _Tail:
    """One job's whole tail: call the service, report, dispose the spool.

    Run off the belt on a worker thread, so several downloads (and several
    users) progress at once instead of queueing behind one. The steps are
    stateless, so sharing them across workers is safe.
    """

    call: Step
    route: Sink
    shelve: Sink

    def run(self, env: Envelope) -> None:
        """Process one envelope end to end; a crash is logged, not lost."""
        try:
            done = Envelope(env.job, self.call.process(env.job))
            self.route.handle(done)
            self.shelve.handle(done)
        except Exception:
            logging.getLogger('relay').exception(
                'worker_crashed src=%s', env.job.src
            )


class Dispatch(Sink):
    """Hand each job to a bounded worker pool; never block the belt.

    The belt keeps reading updates (so every sender is acked at once) while
    downloads run concurrently, up to the pool size -- the multi-user,
    many-links path.
    """

    def __init__(self, pool: ThreadPoolExecutor, tail: _Tail) -> None:
        self._pool = pool
        self._tail = tail

    def handle(self, env: Envelope) -> None:
        """Submit the job to the pool and return at once."""
        self._pool.submit(self._tail.run, env)


def _spec(cfg: Settings, name: str, env: Mapping[str, str]) -> TgSpec:
    """The Telegram dock spec for this bot (spool, offset, chats, ack)."""
    spool = cfg.bot_dir(name) / '_spool'
    return TgSpec(
        spool=SpoolSpec(into=spool, budget=functools.partial(free_quota, cfg)),
        dest=spool,
        offset=cfg.state / f'{name}.offset',
        chats=chats_from(env),
        help=env.get('RELAY_HELP', _DEFAULT_HELP),
        ack=_ACKS.get(name, _DEFAULT_ACK),
    )


def build(cfg: Settings, env: Mapping[str, str]) -> Stage:
    """Assemble the relay belt; secrets come from the passed mapping."""
    api = TgApi(env.get('TG_TOKEN', ''))
    name = _name(env)
    spool = cfg.bot_dir(name) / '_spool'
    docks = merge_watch(
        _dock(env, api, _spec(cfg, name, env)),
        FolderSpec(
            root=cfg.bot_dir(name),
            dest=spool,
            exts=_exts(env),
            poll_sec=cfg.poll_sec,
        ),
        SeenPaths(cfg.seen_paths_max),
    )
    channel = TgChannel(api)
    style = style_for(env)
    tail = _Tail(
        call=_call(env, channel, style),
        route=RouteOrigin(tg=TgStatus(channel, style), loc=Null()),
        shelve=Shelve(cfg.bot_done(name), spooled_or_dropped),
    )
    if env.get('RELAY_DOCK', 'media') == 'links':
        pool = ThreadPoolExecutor(
            max_workers=_workers(env), thread_name_prefix=f'{name}-dl'
        )
        return docks >> Dispatch(pool, tail)
    return docks >> tail.call >> tail.route >> tail.shelve


def main(env: Mapping[str, str] | None = None) -> int:
    """Build Settings once and drain the relay belt."""
    mapping = os.environ if env is None else env
    cfg = load(mapping)
    (cfg.bot_dir(_name(mapping)) / '_spool').mkdir(parents=True, exist_ok=True)
    return run(_name(mapping), build(cfg, mapping), cfg.logs)


if __name__ == '__main__':
    raise SystemExit(main())
