"""frames bot: a video (file or link) becomes its every-Nth frames.

Graph: (TgLinks | TgMedia) -> FetchLink -> ExtractFrames ->
SendResult -> Reply -> DisposeSource. FetchLink passes media files
through untouched, so both docks share one belt.
"""

from __future__ import annotations

import functools
import os
from typing import TYPE_CHECKING

from minion_core.adapters import video
from minion_core.adapters.fetch import FetchLink
from minion_core.adapters.files import free_quota
from minion_core.adapters.tg import SpoolSpec
from minion_core.adapters.tg import TgAny
from minion_core.adapters.tg import TgApi
from minion_core.adapters.tg import TgChannel
from minion_core.adapters.tg import TgSpec
from minion_core.adapters.tg import chats_from
from minion_core.adapters.tg import spool_of
from minion_core.kernel import DisposeSource
from minion_core.kernel import Disposition
from minion_core.kernel import Reply
from minion_core.kernel import SendResult
from minion_core.kernel import Step
from minion_core.kernel import Verdict
from minion_core.kernel import run
from minion_core.settings import load

if TYPE_CHECKING:
    from collections.abc import Mapping

    from minion_core.kernel import Job
    from minion_core.kernel import Stage
    from minion_core.settings import Settings

BOT = 'frames'


class ExtractFrames(Step):
    """The bot's one transformation: video -> frame directory."""

    def __init__(self, cfg: Settings) -> None:
        self._cfg = cfg

    def process(self, job: Job) -> Verdict:
        """Extract every ``frame_stride``-th frame."""
        out = job.dest / f'{job.src.stem}_frames'
        spec = video.FrameSpec(
            stride=self._cfg.frame_stride,
            timeout_sec=self._cfg.download_timeout_sec,
        )
        try:
            shots = video.frames(job.src, out, spec)
        except video.ProbeError:
            return Verdict(Disposition.FAILED, reason='probe_failed')
        if not shots:
            return Verdict(Disposition.FAILED, reason='probe_failed')
        return Verdict(
            Disposition.DELIVERED, result=out, reply=f'{len(shots)} frames'
        )


def build(cfg: Settings, env: Mapping[str, str]) -> Stage:
    """Assemble the belt: one dock accepts links and videos.

    One token allows one getUpdates consumer, so both kinds share
    the TgAny dock instead of merging two Telegram docks.
    """
    api = TgApi(env.get('TG_TOKEN', ''))
    spec = TgSpec(
        spool=SpoolSpec(
            into=cfg.bot_dir(BOT), budget=functools.partial(free_quota, cfg)
        ),
        dest=cfg.bot_dir(BOT),
        offset=cfg.state / f'{BOT}.offset',
        chats=chats_from(env),
        kinds=('video', 'document'),
    )
    channel = TgChannel(api)
    return (
        TgAny(api, spec)
        >> FetchLink(cfg)
        >> ExtractFrames(cfg)
        >> SendResult(channel)
        >> Reply(channel)
        >> DisposeSource(spool_of)
    )


def main(env: Mapping[str, str] | None = None) -> int:
    """Build Settings once and drain the belt."""
    mapping = os.environ if env is None else env
    cfg = load(mapping)
    return run(BOT, build(cfg, mapping), cfg.logs)


if __name__ == '__main__':
    raise SystemExit(main())
