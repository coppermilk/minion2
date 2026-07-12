"""The telegram container's per-belt supervisor: restart with backoff."""

from __future__ import annotations

import threading

from minions import telegram


def test_next_delay_resets_after_a_healthy_run() -> None:
    """A belt that ran long enough drops the backoff to the start."""
    got = telegram._next_delay(16.0, ran_sec=telegram._HEALTHY_RUN_SEC + 1)
    assert got == telegram._BACKOFF_START_SEC


def test_next_delay_grows_and_caps() -> None:
    """A fast exit doubles the delay, up to the cap."""
    cap = telegram._BACKOFF_MAX_SEC
    assert telegram._next_delay(2.0, ran_sec=0.0) == 4.0
    assert telegram._next_delay(999.0, ran_sec=0.0) == cap


def test_supervisor_restarts_the_belt_until_stopped(monkeypatch) -> None:
    """A belt that keeps exiting is re-run, then the stop event ends it."""
    monkeypatch.setattr(telegram, '_BACKOFF_START_SEC', 0.0)  # no real waits
    stop = threading.Event()
    runs: list[int] = []

    def fake_run_once(_bot, _log) -> None:
        runs.append(1)
        if len(runs) >= 3:
            stop.set()

    monkeypatch.setattr(telegram, '_run_once', fake_run_once)
    bot = telegram._Bot(cfg=None, name='x', env={})  # type: ignore[arg-type]
    telegram._supervise(bot, stop)
    assert len(runs) == 3  # ran, restarted, restarted -> stop
