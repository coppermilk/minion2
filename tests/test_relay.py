"""relay: per-bot ack text, and the one self-editing status message."""

from __future__ import annotations

from typing import TYPE_CHECKING

from minion_core.kernel import Disposition
from minion_core.kernel import Envelope
from minion_core.kernel import Job
from minion_core.kernel import Origin
from minion_core.kernel import Verdict
from minions.telegram.progress_style import STYLES
from minions.telegram.relay import _ACKS
from minions.telegram.relay import TgStatus

if TYPE_CHECKING:
    from pathlib import Path


def test_each_media_bot_has_its_own_ack() -> None:
    """The six relay bots each get a distinct, non-empty ack."""
    assert set(_ACKS) == {
        'censor-blur',
        'censor-black',
        'restore',
        'frames',
        'fetch',
        'fan-save',
    }
    assert all(_ACKS.values())  # none empty
    assert len(set(_ACKS.values())) == len(_ACKS)  # each distinct


class _FakeChannel:
    """Records edits and file sends instead of touching Telegram."""

    def __init__(self) -> None:
        self.edits: list[str] = []
        self.files: list[Path] = []

    def edit_text(self, _origin: Origin, text: str) -> None:
        self.edits.append(text)

    def send_file(self, _origin: Origin, path: Path) -> None:
        self.files.append(path)


def _job(tmp_path: Path) -> Job:
    src = tmp_path / 'in.url'
    src.write_bytes(b'x')
    return Job(
        src=src,
        dest=tmp_path,
        stem='in',
        origin=Origin('tg', '1:2:9:/spool/in.url'),
    )


def test_status_delivers_then_marks_done(tmp_path: Path) -> None:
    """On delivery: edit to sending, upload the file, edit to done."""
    channel = _FakeChannel()
    result = tmp_path / 'out.mp4'
    result.write_bytes(b'video')
    env = Envelope(
        _job(tmp_path), Verdict(Disposition.DELIVERED, result=result)
    )
    TgStatus(channel, STYLES['blocks']).handle(env)  # type: ignore[arg-type]
    assert channel.files == [result]  # the video was sent
    assert len(channel.edits) == 2  # sending, then done
    assert channel.edits[-1]  # a non-empty terminal 'done'


def test_status_reports_failure_in_the_same_message(tmp_path: Path) -> None:
    """A failure edits the ack to the reason -- never silent, no file."""
    channel = _FakeChannel()
    env = Envelope(
        _job(tmp_path),
        Verdict(Disposition.FAILED, reason='boom', reply='Sorry, offline.'),
    )
    TgStatus(channel, STYLES['blocks']).handle(env)  # type: ignore[arg-type]
    assert channel.files == []  # nothing delivered
    assert channel.edits == ['Sorry, offline.']  # terminal error, always


def test_status_failure_without_reply_still_tells_the_sender(
    tmp_path: Path,
) -> None:
    """Even a reply-less failure edits the ack to a default apology."""
    channel = _FakeChannel()
    env = Envelope(_job(tmp_path), Verdict(Disposition.FAILED, reason='x'))
    TgStatus(channel, STYLES['blocks']).handle(env)  # type: ignore[arg-type]
    assert len(channel.edits) == 1
    assert channel.edits[0]  # a non-empty apology, not silence


def test_throttle_limits_edits_to_a_new_step_bucket() -> None:
    """An edit fires only when the percent crosses a STEP boundary."""
    from minions.telegram.relay import _Throttle

    throttle = _Throttle(step=5, min_sec=0.0)  # interval never blocks
    assert throttle.due(0)  # first bucket
    assert not throttle.due(3)  # same bucket (0)
    assert throttle.due(5)  # new bucket (1)
    assert not throttle.due(6)  # same bucket (1)
    assert throttle.due(10)  # new bucket (2)


def test_throttle_respects_the_minimum_interval() -> None:
    """Even a new bucket waits out the minimum edit interval."""
    from minions.telegram.relay import _Throttle

    throttle = _Throttle(step=5, min_sec=999.0)
    assert throttle.due(0)
    assert not throttle.due(50)  # new bucket, but too soon


def test_call_service_live_edits_the_bar_on_progress(tmp_path: Path) -> None:
    """The live step edits the ack as the download reports progress."""
    from minion_core.adapters.service_call import ServiceCall
    from minions.telegram.relay import CallServiceLive

    channel = _FakeChannel()
    live = CallServiceLive(ServiceCall('http://x'), channel, STYLES['blocks'])  # type: ignore[arg-type]

    class _FakeClient:
        def run(self, src, _dest, on_progress):
            on_progress(50)
            return Verdict(Disposition.DELIVERED, result=src)

    live._client = _FakeClient()  # type: ignore[assignment]
    src = tmp_path / 'in.url'
    src.write_bytes(b'x')
    job = Job(
        src=src,
        dest=tmp_path,
        stem='in',
        origin=Origin('tg', '1:2:9:/spool/in.url'),
    )
    verdict = live.process(job)
    assert verdict.disposition is Disposition.DELIVERED
    assert channel.edits  # the progress bar was drawn at least once
