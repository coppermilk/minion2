"""censor bot: people in a photo are hidden before it goes back.

Graph: TgMedia(photo) -> HidePeople -> SendResult -> Reply ->
DisposeSource. Mode axis (BLUEPRINT 9): blur / black / restore
(blur, then the LLM repaints the hidden background).

Correctness is CT-B: a missed person leaks the hidden subject, so
detections are applied verbatim and zero detections is a SKIP, never
a silent pass-through of the original.
"""

from __future__ import annotations

import functools
import os
from typing import TYPE_CHECKING

from minion_core.adapters import llm
from minion_core.adapters import vision
from minion_core.adapters.files import HideSpec
from minion_core.adapters.files import free_quota
from minion_core.adapters.files import hide_boxes
from minion_core.adapters.tg import SpoolSpec
from minion_core.adapters.tg import TgApi
from minion_core.adapters.tg import TgChannel
from minion_core.adapters.tg import TgMedia
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
from minion_core.settings import MODE_RESTORE
from minion_core.settings import load

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from minion_core.kernel import Job
    from minion_core.kernel import Stage
    from minion_core.settings import Settings

BOT = 'censor'


class HidePeople(Step):
    """The bot's one transformation: hide every detected person."""

    def __init__(self, cfg: Settings, spec: llm.LlmSpec) -> None:
        self._cfg = cfg
        self._llm = spec

    def process(self, job: Job) -> Verdict:
        """Detect, hide, and optionally restore the background."""
        boxes = vision.person_boxes(job.src)
        if not boxes:
            return Verdict(
                Disposition.SKIPPED,
                reason='no_person',
                reply='no people found',
            )
        out = job.dest / f'{job.src.stem}_s1{job.src.suffix}'
        hide_boxes(
            job.src, out, HideSpec(boxes=boxes, mode=self._cfg.censor_mode)
        )
        final = self._maybe_restore(out)
        return Verdict(Disposition.DELIVERED, result=final, reply='censored')

    def _maybe_restore(self, hidden: Path) -> Path:
        if self._cfg.censor_mode != MODE_RESTORE:
            return hidden
        return llm.restore_background(hidden, self._llm)


def build(cfg: Settings, env: Mapping[str, str]) -> Stage:
    """Assemble the belt; secrets come from the passed mapping."""
    api = TgApi(env.get('TG_TOKEN', ''))
    spec = TgSpec(
        spool=SpoolSpec(
            into=cfg.bot_dir(BOT), budget=functools.partial(free_quota, cfg)
        ),
        dest=cfg.bot_dir(BOT),
        offset=cfg.state / f'{BOT}.offset',
        chats=chats_from(env),
        kinds=('photo', 'document'),
    )
    channel = TgChannel(api)
    return (
        TgMedia(api, spec)
        >> HidePeople(cfg, llm.spec_from(env))
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
