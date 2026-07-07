"""The belt (BLUEPRINT 5): one data type, one operator, three kinds.

Fault containment: every Step is crash-guarded (REQ-KRN-001), every
Source runs on its own daemon thread behind a bounded queue
(REQ-KRN-003), disposal Sinks act only on a decided delivery
(REQ-KRN-004). Telemetry is a kernel service: ``run`` logs every
non-DELIVERED disposition with its stable reason code (REQ-OBS-001).

Placement note (waiver register): ``next_free_path``, ``atomic_write``
and ``move_atomic`` are CT-A file guarantees that kernel sinks rely
on, so they live here (the kernel imports stdlib only);
``adapters.files`` re-exports them as the documented API surface
(REQ-DATA-001/002).
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import sys
import tempfile
import threading
from abc import ABC
from abc import abstractmethod
from collections import OrderedDict
from dataclasses import dataclass
from dataclasses import replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Protocol
from typing import TypeAlias
from typing import cast

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterator

Stream: TypeAlias = 'Iterator[Envelope]'
Emit: TypeAlias = 'Callable[[Envelope], None]'

QUEUE_DEPTH = 64
"""Fixed source-to-belt buffer depth (BLUEPRINT 10, REQ-KRN-003)."""

NAME_TRIES = 10_000
"""Collision-suffix search bound (all loops bounded, BLUEPRINT 4)."""

_END = object()


class Disposition(Enum):
    """Terminal fate of a job at each stage boundary."""

    DELIVERED = 'delivered'
    SKIPPED = 'skipped'
    REJECTED = 'rejected'
    FAILED = 'failed'


@dataclass(frozen=True)
class Origin:
    """Transport-neutral provenance; ``ref`` is source-defined.

    Convention: a source that wants its input disposed of after
    delivery sets ``ref`` to the absolute path it spooled; any other
    ref is opaque to the kernel and never resolves to a file.
    """

    source: str
    ref: str


@dataclass(frozen=True)
class Job:
    """One unit of work moving along the belt."""

    src: Path
    dest: Path
    stem: str
    origin: Origin


@dataclass(frozen=True)
class Verdict:
    """A Step's decision about one Job.

    ``reason`` is a stable code, 1:1 with the OPERATIONS failure
    table (REQ-OBS-001).
    """

    disposition: Disposition
    reason: str = ''
    result: Path | None = None
    reply: str = ''
    dest: Path | None = None


@dataclass(frozen=True)
class Envelope:
    """A Job plus the latest Verdict about it."""

    job: Job
    verdict: Verdict | None = None


class Channel(Protocol):
    """A bot's reply identity; adapters provide the transport."""

    def send_text(self, origin: Origin, text: str) -> None:
        """Send a text reply toward the job's origin."""

    def send_file(self, origin: Origin, path: Path) -> None:
        """Send a file toward the job's origin."""


class Stage(ABC):
    """One belt segment: transforms a lazy stream of envelopes."""

    @abstractmethod
    def __call__(self, up: Stream) -> Stream:
        """Consume the upstream and yield the downstream."""

    def __rshift__(self, nxt: Stage) -> Stage:
        """Compose: ``a >> b`` runs ``a``, then ``b``."""
        return _Chain(self, nxt)

    def __or__(self, other: Stage) -> Stage:
        """Merge: ``a | b`` is two docks feeding one belt."""
        return _Merge(self, other)


class _Chain(Stage):
    """Sequential composition of two stages."""

    def __init__(self, first: Stage, second: Stage) -> None:
        self._first = first
        self._second = second

    def __call__(self, up: Stream) -> Stream:
        """Feed the first stage's output to the second."""
        return self._second(self._first(up))


class _Merge(Stage):
    """Two docks, one belt: interleave two head stages."""

    def __init__(self, left: Stage, right: Stage) -> None:
        self._sides = (left, right)

    def __call__(self, _up: Stream) -> Stream:
        """Pump both sides into one bounded queue and drain it."""
        out: queue.Queue[object] = queue.Queue(maxsize=QUEUE_DEPTH)
        for side in self._sides:
            _pump(side(iter(())), out)
        return _drain(out, ends=len(self._sides))


def _pump(stream: Stream, out: queue.Queue[object]) -> None:
    """Drive a stream into a queue from a daemon thread."""

    def work() -> None:
        try:
            for env in stream:
                out.put(env)
        finally:
            out.put(_END)

    threading.Thread(target=work, daemon=True).start()


def _drain(out: queue.Queue[object], ends: int) -> Stream:
    """Yield queued envelopes until every producer has ended."""
    left = ends
    while left:
        item = out.get()
        if item is _END:
            left -= 1
            continue
        yield cast('Envelope', item)


class Source(Stage):
    """A dock: one daemon thread bridges a blocking loop to the belt.

    ``emit`` is a bounded ``queue.Queue.put`` -- a full belt blocks
    the producer, which is the backpressure of REQ-KRN-003.
    """

    def __init__(self, depth: int = QUEUE_DEPTH) -> None:
        self._depth = depth
        self._stop = threading.Event()

    @abstractmethod
    def produce(self, emit: Emit) -> None:
        """Run the blocking loop; call ``emit`` per new envelope."""

    def stop(self) -> None:
        """Ask the producing loop to end (shutdown and tests)."""
        self._stop.set()

    @property
    def stopped(self) -> bool:
        """Whether a stop was requested."""
        return self._stop.is_set()

    def wait(self, sec: float) -> None:
        """Sleep, but wake immediately on stop."""
        self._stop.wait(sec)

    def __call__(self, _up: Stream) -> Stream:
        """Bridge ``produce`` into a lazy stream (REQ-KRN-003)."""
        out: queue.Queue[object] = queue.Queue(maxsize=self._depth)

        def work() -> None:
            try:
                self.produce(out.put)
            except Exception:
                logging.getLogger(type(self).__name__).exception(
                    'source_crashed',
                )
            finally:
                out.put(_END)

        threading.Thread(target=work, daemon=True).start()
        yield from _drain(out, ends=1)


class Step(Stage):
    """A worker: ``job -> verdict``, crash-guarded, short-circuiting.

    A DELIVERED verdict advances the frozen job by constructing its
    successor: ``src <- verdict.result``; ``dest <- verdict.dest``
    when set.
    """

    @abstractmethod
    def process(self, job: Job) -> Verdict:
        """Apply the bot's one small transformation."""

    def __call__(self, up: Stream) -> Stream:
        """Advance every envelope that is still in play."""
        for env in up:
            yield self._advance(env)

    def _advance(self, env: Envelope) -> Envelope:
        if _blocked(env.verdict):
            return env  # REQ-KRN-002: bypass all later Steps
        verdict = self._guarded(env.job)
        return Envelope(_next_job(env.job, verdict), verdict)

    def _guarded(self, job: Job) -> Verdict:
        try:
            return self.process(job)
        except Exception:
            logging.getLogger(type(self).__name__).exception(
                'step_crashed src=%s',
                job.src,
            )
            return Verdict(Disposition.FAILED, reason='step_crashed')


def _blocked(verdict: Verdict | None) -> bool:
    """Whether later Steps must be bypassed (REQ-KRN-002)."""
    if verdict is None:
        return False
    return verdict.disposition is not Disposition.DELIVERED


def _next_job(job: Job, verdict: Verdict) -> Job:
    """Construct the successor of a frozen job."""
    src = verdict.result if verdict.result is not None else job.src
    dest = verdict.dest if verdict.dest is not None else job.dest
    return replace(job, src=src, dest=dest)


class Sink(Stage):
    """A tail: one side effect per envelope, re-emitting."""

    @abstractmethod
    def handle(self, env: Envelope) -> None:
        """Perform the side effect for one envelope."""

    def __call__(self, up: Stream) -> Stream:
        """Handle each envelope, guarded, and pass it on."""
        for env in up:
            self._guarded(env)
            yield env

    def _guarded(self, env: Envelope) -> None:
        try:
            self.handle(env)
        except Exception:
            logging.getLogger(type(self).__name__).exception(
                'sink_crashed src=%s',
                env.job.src,
            )


def _delivered(env: Envelope) -> bool:
    """Whether the envelope carries a decided, successful delivery."""
    if env.verdict is None:
        return False
    return env.verdict.disposition is Disposition.DELIVERED


class Null(Sink):
    """A sink that does nothing -- the unused side of a RouteOrigin."""

    def handle(self, env: Envelope) -> None:
        """No side effect (e.g. a loc-origin result reaches no chat)."""


class Reply(Sink):
    """Send ``verdict.reply`` back through the bot's channel."""

    def __init__(self, channel: Channel) -> None:
        self._channel = channel

    def handle(self, env: Envelope) -> None:
        """Reply when the verdict carries text."""
        if env.verdict is not None and env.verdict.reply:
            self._channel.send_text(env.job.origin, env.verdict.reply)


class SendResult(Sink):
    """Send the delivered result file(s) through the channel."""

    def __init__(self, channel: Channel) -> None:
        self._channel = channel

    def handle(self, env: Envelope) -> None:
        """Send the result; a directory result sends each file."""
        if not _delivered(env) or env.verdict is None:
            return
        result = env.verdict.result
        if result is None:
            return
        for path in _result_files(result):
            self._channel.send_file(env.job.origin, path)


def _result_files(result: Path) -> list[Path]:
    """The files a result stands for (itself, or its children)."""
    if result.is_dir():
        return sorted(p for p in result.iterdir() if p.is_file())
    return [result] if result.is_file() else []


class ArchiveTo(Sink):
    """Move a delivered result into an archive directory."""

    def __init__(self, into: Path) -> None:
        self._into = into

    def handle(self, env: Envelope) -> None:
        """Archive the result, collision-free (REQ-DATA-001)."""
        if not _delivered(env) or env.verdict is None:
            return
        result = env.verdict.result
        if result is None or not result.is_file():
            return
        move_atomic(result, next_free_path(self._into / result.name))


def _ref_path(origin: Origin) -> Path | None:
    """Default locator: the whole ref is the disposable path."""
    return Path(origin.ref)


class DisposeSource(Sink):
    """Remove the consumed source once delivery succeeded.

    Composed last, so a FAILED or REJECTED job leaves its source
    untouched (REQ-KRN-004). The kernel never parses refs: a source's
    adapter supplies ``locate`` to map its own ref format to the
    disposable file; the default treats the whole ref as a path.
    """

    def __init__(
        self,
        locate: Callable[[Origin], Path | None] = _ref_path,
    ) -> None:
        self._locate = locate

    def handle(self, env: Envelope) -> None:
        """Unlink the located source file of a delivered envelope."""
        if not _delivered(env):
            return
        path = self._locate(env.job.origin)
        if path is not None and path.is_file():
            path.unlink()


class RouteOrigin(Sink):
    """Delegate to exactly one sink by the job's origin source.

    Two docks, one belt (REQ-DOCK-001): a tg-origin result reaches
    the chat sink, a loc-origin result reaches the folder sink -- no
    bot code branches on origin.
    """

    def __init__(self, tg: Sink, loc: Sink) -> None:
        self._tg = tg
        self._loc = loc

    def handle(self, env: Envelope) -> None:
        """Hand the envelope to the sink of its origin."""
        side = self._tg if env.job.origin.source == 'tg' else self._loc
        side.handle(env)


class SeenPaths:
    """Thread-safe LRU set of already-emitted paths (bounded)."""

    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def add(self, path: Path) -> bool:
        """Record the path; return True when it is new."""
        key = str(path)
        with self._lock:
            if key in self._seen:
                self._seen.move_to_end(key)
                return False
            self._seen[key] = None
            while len(self._seen) > self._cap:
                self._seen.popitem(last=False)
            return True

    def has(self, path: Path) -> bool:
        """Whether the path was already emitted (non-mutating)."""
        with self._lock:
            return str(path) in self._seen


@dataclass(frozen=True)
class FolderSpec:
    """What a Folder dock watches and where its jobs deliver."""

    root: Path
    dest: Path
    exts: tuple[str, ...]
    poll_sec: float = 2.0
    once: bool = False


PENDING_MAX = 1024
"""Bound on the stability guard's size memory (bounded memory)."""

BATCH_SCANS = 2
"""Scans a once-mode dock runs: sight, then the stability pass."""


class Folder(Source):
    """Emit each new matching file under ``spec.root`` (dock 'loc').

    Write-stability guard (REQ-KRN-005): a path is emitted only when
    its size is unchanged across two consecutive scans, so a file
    still being copied into the watch dir is never consumed torn.
    Once-mode therefore runs exactly two scans.
    """

    def __init__(self, spec: FolderSpec, seen: SeenPaths) -> None:
        super().__init__()
        self._spec = spec
        self._seen = seen
        self._pending: OrderedDict[str, int] = OrderedDict()
        self._scans = 0

    def produce(self, emit: Emit) -> None:
        """Poll the folder; bounded by stop() or ``spec.once``."""
        while True:
            self._scan(emit)
            if self.stopped:
                return
            if self._spec.once and self._scans >= BATCH_SCANS:
                return
            if not self._spec.once:
                self.wait(self._spec.poll_sec)

    def _scan(self, emit: Emit) -> None:
        self._scans += 1
        for path in sorted(self._spec.root.glob('*')):
            if path.suffix.lower() not in self._spec.exts:
                continue
            if self._seen.has(path):
                continue
            if self._stable(path):
                self._seen.add(path)
                emit(_folder_envelope(path, self._spec.dest))

    def _stable(self, path: Path) -> bool:
        """REQ-KRN-005: only a size unchanged across polls is whole."""
        key = str(path)
        size = path.stat().st_size
        if self._pending.pop(key, None) == size:
            return True
        self._pending[key] = size
        while len(self._pending) > PENDING_MAX:
            self._pending.popitem(last=False)
        return False


def _folder_envelope(path: Path, dest: Path) -> Envelope:
    """Wrap a discovered file as a fresh local-origin envelope."""
    origin = Origin(source='loc', ref=str(path))
    job = Job(src=path, dest=dest, stem=path.stem, origin=origin)
    return Envelope(job)


def merge_watch(tg: Stage, watch: FolderSpec | None, seen: SeenPaths) -> Stage:
    """The tg dock, merged with a watch dock when configured.

    Two docks, one belt (REQ-DOCK-001): an absent watch dir keeps
    the belt Telegram-only with zero caller branching.
    """
    if watch is None:
        return tg
    return tg | Folder(watch, seen)


def next_free_path(path: Path) -> Path:
    """First non-colliding sibling: name, name_2, ... (REQ-DATA-001)."""
    if not path.exists():
        return path
    for n in range(2, NAME_TRIES):
        cand = path.with_stem(f'{path.stem}_{n}')
        if not cand.exists():
            return cand
    raise FileExistsError(f'name_collision unresolved: {path}')


def atomic_write(path: Path, data: bytes) -> None:
    """Write via same-directory temp + rename (REQ-DATA-002)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(dir=path.parent, suffix='.part')
    tmp = Path(raw)
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(data)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def move_atomic(src: Path, dst: Path) -> Path:
    """Move without torn output; copy+rename across devices."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        src.rename(dst)
    except OSError:  # cross-device: stage next to dst, then rename
        tmp = dst.with_name(dst.name + '.part')
        try:
            shutil.copy2(src, tmp)
            tmp.replace(dst)
        finally:
            tmp.unlink(missing_ok=True)
        src.unlink()
    return dst


_LOG_FMT = logging.Formatter('%(asctime)s %(name)s %(message)s')
"""Every line carries the time and the bot name (REQ-OBS-001)."""


def bot_logger(name: str, logs: Path | None) -> logging.Logger:
    """One log to two places: the container stdout AND logs/<name>.log.

    The stdout handler sits on the ROOT logger, so EVERY logger reaches
    ``docker logs`` (Container Manager's Log tab) as the complete,
    authoritative view -- this bot's records and the kernel's per-class
    crash guards (``step_crashed``/``source_crashed``, logged under
    ``type(self).__name__``) alike. The named bot logger keeps only its
    FileHandler and propagates to root for stdout, so nothing prints
    twice. Every line is time-stamped. Idempotent: each handler is
    installed once per process.
    """
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(
        isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
        for h in root.handlers
    ):
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(_LOG_FMT)
        root.addHandler(stream)
    log = logging.getLogger(name)
    log.setLevel(logging.INFO)
    if logs is not None and not log.handlers:
        logs.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(logs / f'{name}.log')
        handler.setFormatter(_LOG_FMT)
        log.addHandler(handler)
    return log


def run(name: str, graph: Stage, logs: Path | None = None) -> int:
    """Drain the graph -- the single sanctioned unbounded loop.

    A daemon source never ends (drain forever); a batch source ends
    (drain once, exit). Clean stop -> 0; fatal init -> non-zero.
    """
    log = bot_logger(name, logs)
    try:
        stream = graph(iter(()))
    except Exception:
        log.exception('fatal_init')
        return 1
    for env in stream:
        _log_env(log, env)
    log.info('drained')
    return 0


def _log_env(log: logging.Logger, env: Envelope) -> None:
    """Log every disposition; non-DELIVERED with its reason code."""
    verdict = env.verdict
    if verdict is None:
        return
    if verdict.disposition is Disposition.DELIVERED:
        log.info('delivered src=%s result=%s', env.job.src, verdict.result)
        return
    log.warning(
        '%s reason=%s src=%s',
        verdict.disposition.value,
        verdict.reason,
        env.job.src,
    )
