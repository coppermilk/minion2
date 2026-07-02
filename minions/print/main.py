"""print bot: a PDF dropped into ``print/`` reaches the printer.

Graph: Folder(print/) -> PrintPdf -> ArchiveTo(print/_done/).
Printing goes through the ``lp`` command (stdlib subprocess -- no
vendor SDK, so the step lives with its bot).
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

from minion_core.kernel import ArchiveTo
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

BOT = 'print'

LP = 'lp'
"""The spooler command; CUPS owns the actual printer."""

LP_TIMEOUT_SEC = 120
"""Wall-time bound on handing a file to the spooler."""


class PrintPdf(Step):
    """The bot's one transformation: hand the PDF to the spooler."""

    def process(self, job: Job) -> Verdict:
        """Print; the file itself is the delivered result."""
        try:
            proc = subprocess.run(  # noqa: S603 -- fixed binary, no shell
                [LP, str(job.src)],
                capture_output=True,
                timeout=LP_TIMEOUT_SEC,
                check=False,
            )
        except FileNotFoundError:
            return Verdict(Disposition.FAILED,
                           reason='printer_missing')
        except subprocess.TimeoutExpired:
            return Verdict(Disposition.FAILED, reason='print_timeout')
        if proc.returncode != 0:
            return Verdict(Disposition.FAILED, reason='print_failed')
        return Verdict(Disposition.DELIVERED, result=job.src,
                       reply=f'printed {job.src.name}')


def build(cfg: Settings) -> Stage:
    """Assemble the belt: watch the queue, print, archive."""
    spec = FolderSpec(root=cfg.print_queue, dest=cfg.print_done,
                      exts=('.pdf',), poll_sec=cfg.poll_sec)
    seen = SeenPaths(cfg.seen_paths_max)
    return (Folder(spec, seen) >> PrintPdf()
            >> ArchiveTo(cfg.print_done))


def main(env: Mapping[str, str] | None = None) -> int:
    """Build Settings once and drain the belt."""
    mapping = os.environ if env is None else env
    cfg = load(mapping)
    return run(BOT, build(cfg), cfg.logs)


if __name__ == '__main__':
    raise SystemExit(main())
