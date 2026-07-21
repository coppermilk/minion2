"""print bot: a PDF dropped into ``print/`` reaches the printer.

Graph: Folder(print/) -> PrintPdf -> ArchiveTo(print/_done/).
The spooler is a Settings axis (REQ-PRT-001): ``lp`` on the NAS,
SumatraPDF on Windows -- the software never branches on the host OS
(BLUEPRINT 1.2); the deployment's .env decides.
"""

from __future__ import annotations

import logging
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


class PrintPdf(Step):
    """The bot's one transformation: hand the PDF to the spooler.

    The argv is ``[*cfg.print_spooler, <pdf path>]`` -- the spooler
    is configuration, never a host-OS branch (REQ-PRT-001).
    """

    def __init__(self, cfg: Settings) -> None:
        self._cfg = cfg

    def process(self, job: Job) -> Verdict:
        """Print; the file itself is the delivered result."""
        try:
            proc = subprocess.run(  # noqa: S603 -- configured argv, no shell
                [*self._cfg.print_spooler, str(job.src)],
                capture_output=True,
                timeout=self._cfg.print_timeout_sec,
                check=False,
            )
        except FileNotFoundError:
            spooler = self._cfg.print_spooler
            argv0 = spooler[0] if spooler else '?'
            logging.getLogger(BOT).warning(
                'printer_missing spooler=%s -- install SumatraPDF or set '
                'PRINT_SPOOLER',
                argv0,
            )
            return Verdict(Disposition.FAILED, reason='printer_missing')
        except subprocess.TimeoutExpired:
            return Verdict(Disposition.FAILED, reason='print_timeout')
        if proc.returncode != 0:
            return Verdict(Disposition.FAILED, reason='print_failed')
        return Verdict(
            Disposition.DELIVERED,
            result=job.src,
            reply=f'printed {job.src.name}',
        )


def build(cfg: Settings) -> Stage:
    """Assemble the belt: watch the queue, print, archive."""
    spec = FolderSpec(
        root=cfg.print_queue,
        dest=cfg.print_done,
        exts=('.pdf',),
        poll_sec=cfg.poll_sec,
    )
    seen = SeenPaths(cfg.seen_paths_max)
    return Folder(spec, seen) >> PrintPdf(cfg) >> ArchiveTo(cfg.print_done)


def main(env: Mapping[str, str] | None = None) -> int:
    """Build Settings once and drain the belt."""
    mapping = os.environ if env is None else env
    cfg = load(mapping)
    return run(BOT, build(cfg), cfg.logs)


if __name__ == '__main__':
    raise SystemExit(main())
