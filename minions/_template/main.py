"""Copy me: rename the package, replace the Step, wire one graph.

The recipe (BLUEPRINT 9): pick a Source (Folder / TgMedia / TgLinks),
write exactly one Step, compose sinks last so disposal happens only
after delivery is decided (REQ-KRN-004).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from minion_core.kernel import Disposition
from minion_core.kernel import Folder
from minion_core.kernel import FolderSpec
from minion_core.kernel import SeenPaths
from minion_core.kernel import Step
from minion_core.kernel import Verdict
from minion_core.kernel import run
from minion_core.settings import load

if TYPE_CHECKING:
    from collections.abc import Mapping

    from minion_core.kernel import Job
    from minion_core.kernel import Stage
    from minion_core.settings import Settings

BOT = 'template'


class Nop(Step):
    """Replace me with the bot's one small transformation."""

    def process(self, job: Job) -> Verdict:
        """Deliver the input unchanged."""
        return Verdict(Disposition.DELIVERED, result=job.src)


def build(cfg: Settings) -> Stage:
    """Assemble the belt: one source, one step, sinks last."""
    spec = FolderSpec(root=cfg.inbox, dest=cfg.inbox,
                      exts=('.txt',), poll_sec=cfg.poll_sec)
    seen = SeenPaths(cfg.seen_paths_max)
    return Folder(spec, seen) >> Nop()


def main(env: Mapping[str, str] | None = None) -> int:
    """Build Settings once and drain the belt."""
    mapping = os.environ if env is None else env
    cfg = load(mapping)
    return run(BOT, build(cfg), cfg.logs)


if __name__ == '__main__':
    raise SystemExit(main())
