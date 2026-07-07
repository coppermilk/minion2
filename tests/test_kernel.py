"""Kernel requirement tests: REQ-KRN-001..004, REQ-OBS-001."""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from minion_core.kernel import DisposeSource
from minion_core.kernel import Disposition
from minion_core.kernel import Envelope
from minion_core.kernel import Job
from minion_core.kernel import Origin
from minion_core.kernel import SeenPaths
from minion_core.kernel import Source
from minion_core.kernel import Step
from minion_core.kernel import Verdict
from minion_core.kernel import bot_logger
from minion_core.kernel import run

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

    from minion_core.kernel import Emit
    from minion_core.kernel import Stage


def make_env_for(path: Path) -> Envelope:
    origin = Origin(source='loc', ref=str(path))
    return Envelope(
        Job(src=path, dest=path.parent, stem=path.stem, origin=origin)
    )


class Fixed(Source):
    """Test dock: emits a fixed list, then ends (batch)."""

    def __init__(self, envs: list[Envelope], depth: int = 64) -> None:
        super().__init__(depth)
        self._envs = envs
        self.emitted = 0

    def produce(self, emit: Emit) -> None:
        for env in self._envs:
            emit(env)
            self.emitted += 1


class Boom(Step):
    """Test step: always raises."""

    def process(self, job: Job) -> Verdict:
        raise RuntimeError('injected fault')


class Spy(Step):
    """Test step: records what reached it, delivers unchanged."""

    def __init__(self) -> None:
        self.seen: list[Path] = []

    def process(self, job: Job) -> Verdict:
        self.seen.append(job.src)
        return Verdict(Disposition.DELIVERED, result=job.src)


def drain(graph: Stage) -> list[Envelope]:
    return list(graph(iter(())))


def test_step_crash_yields_failed_never_kills(tmp_path: Path) -> None:
    """REQ-KRN-001: a raising Step becomes FAILED, daemon survives."""
    envs = [make_env_for(tmp_path / 'a.txt'), make_env_for(tmp_path / 'b.txt')]
    out = drain(Fixed(envs) >> Boom())
    assert len(out) == 2
    for env in out:
        assert env.verdict is not None
        assert env.verdict.disposition is Disposition.FAILED
        assert env.verdict.reason == 'step_crashed'


def test_non_delivered_bypasses_later_steps(tmp_path: Path) -> None:
    """REQ-KRN-002: a non-DELIVERED envelope skips later Steps."""
    spy = Spy()
    out = drain(Fixed([make_env_for(tmp_path / 'a.txt')]) >> Boom() >> spy)
    assert spy.seen == []
    assert out[0].verdict is not None
    assert out[0].verdict.disposition is Disposition.FAILED


def test_source_buffering_is_bounded(tmp_path: Path) -> None:
    """REQ-KRN-003: an unconsumed source blocks at the queue depth."""
    depth = 4
    envs = [make_env_for(tmp_path / f'{i}.txt') for i in range(50)]
    source = Fixed(envs, depth=depth)
    stream = source(iter(()))
    next(stream)  # start the producer, consume one
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        before = source.emitted
        time.sleep(0.05)
        if source.emitted == before:
            break
    # producer is stalled well short of the 50 it wants to emit
    assert source.emitted <= depth + 2


def test_failed_job_leaves_source_intact(tmp_path: Path) -> None:
    """REQ-KRN-004: disposal only after delivery is decided."""
    src = tmp_path / 'keep.txt'
    src.write_bytes(b'data')
    out = drain(Fixed([make_env_for(src)]) >> Boom() >> DisposeSource())
    assert out[0].verdict is not None
    assert out[0].verdict.disposition is Disposition.FAILED
    assert src.exists()


def test_delivered_job_disposes_source(tmp_path: Path) -> None:
    """REQ-KRN-004 counterpart: DELIVERED consumes the source."""
    src = tmp_path / 'gone.txt'
    src.write_bytes(b'data')
    drain(Fixed([make_env_for(src)]) >> Spy() >> DisposeSource())
    assert not src.exists()


def test_non_delivered_logged_with_reason(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """REQ-OBS-001: every non-DELIVERED disposition is logged."""
    graph = Fixed([make_env_for(tmp_path / 'a.txt')]) >> Boom()
    with caplog.at_level('WARNING', logger='obs-test'):
        code = run('obs-test', graph)
    assert code == 0
    assert 'reason=step_crashed' in caplog.text


def test_merge_two_docks_one_belt(tmp_path: Path) -> None:
    """A | b interleaves both docks into one belt."""
    left = Fixed([make_env_for(tmp_path / 'l.txt')])
    right = Fixed([make_env_for(tmp_path / 'r.txt')])
    spy = Spy()
    out = drain((left | right) >> spy)
    assert len(out) == 2
    assert {p.name for p in spy.seen} == {'l.txt', 'r.txt'}


def test_seen_paths_lru_dedup_is_bounded(tmp_path: Path) -> None:
    """Dedup memory stays bounded and drops the oldest entries."""
    seen = SeenPaths(cap=2)
    a, b, c = (tmp_path / n for n in 'abc')
    assert seen.add(a)
    assert not seen.add(a)
    assert seen.add(b)
    assert seen.add(c)  # evicts a
    assert seen.add(a)


def _adder(
    seen: SeenPaths, tmp_path: Path, wins: list[Path]
) -> Callable[[], None]:
    lock = threading.Lock()

    def work() -> None:
        for i in range(100):
            path = tmp_path / str(i)
            if seen.add(path):
                with lock:
                    wins.append(path)

    return work


def _race(work: Callable[[], None]) -> None:
    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_seen_paths_is_thread_safe(tmp_path: Path) -> None:
    """Concurrent adds record each path exactly once."""
    seen = SeenPaths(cap=1000)
    wins: list[Path] = []
    _race(_adder(seen, tmp_path, wins))
    assert len(wins) == 100


def _non_file_streams(logger: logging.Logger) -> list[logging.Handler]:
    return [
        h
        for h in logger.handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
    ]


def test_bot_logger_mirrors_to_stdout_and_file(tmp_path: Path) -> None:
    """Every log reaches the docker stdout; the file keeps the bot's own.

    The stdout handler lives on root (so the kernel's crash guards are
    captured too), the named logger keeps only its file, and nothing
    prints twice. Root's global handler state is saved/restored so the
    assertions are isolated from pytest's own capture handlers.
    """
    root = logging.getLogger()
    saved, saved_level = root.handlers[:], root.level
    root.handlers.clear()
    log = None
    try:
        logs = tmp_path / 'logs'
        log = bot_logger('probe-bot', logs)

        # Root got exactly one stdout stream that mirrors every logger.
        streams = _non_file_streams(root)
        assert len(streams) == 1
        assert streams[0].stream is sys.stdout

        # The named logger keeps only its file handler -- stdout comes
        # from propagation to root, so no line is printed twice.
        assert log.handlers
        assert all(isinstance(h, logging.FileHandler) for h in log.handlers)

        # The record still lands in the on-disk file.
        log.info('probe-message')
        for handler in log.handlers:
            handler.flush()
        text = (logs / 'probe-bot.log').read_text(encoding='ascii')
        assert 'probe-message' in text
        # Every line is time-stamped (YYYY-MM-DD HH:MM:SS ...).
        assert re.search(
            r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.*probe-message', text
        )

        # Idempotent: a second call stacks no duplicate root stdout handler.
        bot_logger('probe-bot', logs)
        assert len(_non_file_streams(root)) == 1
    finally:
        if log is not None:
            for handler in list(log.handlers):
                handler.close()
            log.handlers.clear()
        root.handlers[:] = saved
        root.setLevel(saved_level)
