"""Run a bot from its graph spec: python -m minions.graph <graph.json>.

The kernel-level loader (``minion_core.graph``) assembles the belt from a
parsed spec; this module injects the Phase 0 step catalog (so naming
concrete Steps stays out of the kernel layer) and reads the spec from
disk. A shipped ``minions/<bot>/graph.json`` is the same belt the bot's
``build()`` assembles in code -- the data form is inspectable and
diffable, the seed of the visual layer (ORCHESTRATION.md, Phase 1).

``--events`` wraps every node in a tap and streams one line per boundary
crossing to stdout (Phase 1.5) -- the in-process firehose a live canvas
consumes later.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from minion_core.graph import context
from minion_core.graph import load
from minion_core.kernel import run
from minion_core.settings import load as load_settings
from minions.service import CATALOG

if TYPE_CHECKING:
    from collections.abc import Mapping

    from minion_core.events import Event
    from minion_core.graph import Node
    from minion_core.kernel import Stage
    from minion_core.settings import Settings

EVENTS = '--events'
"""Flag: stream one line per node boundary crossing to stdout."""


def build(spec: Node, cfg: Settings, env: Mapping[str, str]) -> Stage:
    """Assemble the belt a spec describes, using the step catalog."""
    return load(spec, context(cfg, env, spec['bot']), CATALOG)


def read(path: str) -> Node:
    """Parse a graph spec file (JSON)."""
    spec: Node = json.loads(Path(path).read_text(encoding='ascii'))
    return spec


def _emit(event: Event) -> None:
    """Write one event as a line to stdout (the --events firehose)."""
    sys.stdout.write(
        f'{event.ts:.3f} {event.node} {event.phase} {event.disposition}\n'
    )


def main(argv: list[str], env: Mapping[str, str]) -> int:
    """Read a spec, build its belt, and drain it (a daemon blocks)."""
    opts = [arg for arg in argv if arg != EVENTS]
    if not opts:
        sys.stderr.write('usage: python -m minions.graph [--events] <spec>\n')
        return 2
    spec = read(opts[0])
    cfg = load_settings(env)
    ctx = context(cfg, env, spec['bot'])
    if EVENTS in argv:
        ctx = replace(ctx, emit=_emit)
    return run(spec['bot'], load(spec, ctx, CATALOG), cfg.logs)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:], os.environ))
