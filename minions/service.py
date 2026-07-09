"""Service catalog and CLI: run any Step by name over one input file.

    python -m minions.service <step> <input> [<dest>]

The kernel-level dispatcher (``minion_core.service``) does the mechanical
run; this module is where concrete Steps are named, so it -- not the
kernel -- imports the bots (import direction; REQ-ARC-001 skips top-level
``minions/*.py``). Steps that ignore Settings are adapted to the one
factory shape by ``_ignoring_cfg``, so the dispatcher stays uniform. The
catalog is the seed of the graph-as-data layer (ORCHESTRATION.md, Phase 1):
a node is a name here, not a new watcher.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from typing import TypeAlias

from minion_core.adapters.fetch import FetchLink
from minion_core.adapters.files import Deliver
from minion_core.adapters.vision import BlurContour
from minion_core.adapters.vision import HideFaces
from minion_core.adapters.vision import HidePersonBoxes
from minion_core.kernel import Disposition
from minion_core.service import Call
from minion_core.service import invoke
from minion_core.settings import load
from minions.frames.main import ExtractFrames

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Mapping

    from minion_core.kernel import Step
    from minion_core.kernel import Verdict
    from minion_core.settings import Settings

Factory: TypeAlias = 'Callable[[Settings], Step]'
"""A Step constructor over Settings -- the one shape the catalog holds."""

_MIN_ARGV = 2
"""A step name plus an input path -- the least a run needs."""


def _ignoring_cfg(make: Callable[[], Step]) -> Callable[[Settings], Step]:
    """Adapt a no-arg Step to the cfg-taking factory shape."""

    def factory(_cfg: Settings) -> Step:
        return make()

    return factory


CATALOG: dict[str, Factory] = {
    'deliver': _ignoring_cfg(Deliver),
    'censor-black': _ignoring_cfg(HideFaces),
    'censor-blur': _ignoring_cfg(BlurContour),
    'restore-mark': _ignoring_cfg(HidePersonBoxes),
    'fetch': FetchLink,
    'frames': ExtractFrames,
}
"""Every processing service the belt exposes, by name (Phase 0 catalog)."""


def build(name: str, cfg: Settings) -> Step:
    """Construct the named Step; KeyError names the unknown step."""
    return CATALOG[name](cfg)


def run(name: str, call: Call, cfg: Settings) -> Verdict:
    """Build the named Step and invoke it over one input."""
    return invoke(build(name, cfg), call)


def _usage() -> int:
    """Print usage and the known step names to stderr."""
    sys.stderr.write('usage: python -m minions.service <step> <in> [dest]\n')
    sys.stderr.write(f'steps: {", ".join(sorted(CATALOG))}\n')
    return 2


def _line(name: str, verdict: Verdict) -> str:
    """One-line result: name, disposition, then result path or reason."""
    tail = verdict.result if verdict.result is not None else verdict.reason
    return f'{name} {verdict.disposition.value} {tail}\n'


def _exit_code(verdict: Verdict) -> int:
    """0 only for a delivered result; 1 otherwise."""
    return 0 if verdict.disposition is Disposition.DELIVERED else 1


def main(argv: list[str], env: Mapping[str, str]) -> int:
    """Run one step over one file; print the verdict line."""
    if len(argv) < _MIN_ARGV or argv[0] not in CATALOG:
        return _usage()
    src = Path(argv[1])
    dest = Path(argv[2]) if len(argv) > _MIN_ARGV else src.parent
    verdict = run(argv[0], Call(src=src, dest=dest), load(env))
    sys.stdout.write(_line(argv[0], verdict))
    return _exit_code(verdict)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:], os.environ))
