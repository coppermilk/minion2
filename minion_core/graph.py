"""Graph as data: assemble a belt from a spec, not from Python code.

The kernel already runs graphs (``run``); this module only *builds* one
from a parsed spec (a dict, JSON on disk), folding registered nodes with
the same ``>>`` and ``|`` the hand-written ``build()`` functions use. No
new execution engine (ORCHESTRATION.md, Phase 1).

Three node kinds map to three registries. Sources and sinks live in the
kernel/adapters layer, so they are named here; Steps are the Phase 0
catalog, injected by the caller so this module never imports a bot
(import direction; test: adapters never import bots). A shared
``BuildContext`` carries the one Telegram api/channel a bot builds, so a
tg dock and a reply sink share it exactly as ``build()`` does today.

Divergence between bots is a handful of directory aliases (``inbox``,
``spool``, ``bot_dir``, ``bot_done``) plus light per-node knobs -- not a
general configuration language: the IP stays in the Steps.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any
from typing import TypeAlias

from minion_core.adapters.files import Shelve
from minion_core.adapters.files import free_quota
from minion_core.adapters.tg import SpoolSpec
from minion_core.adapters.tg import TgAny
from minion_core.adapters.tg import TgApi
from minion_core.adapters.tg import TgChannel
from minion_core.adapters.tg import TgLinks
from minion_core.adapters.tg import TgMedia
from minion_core.adapters.tg import TgSpec
from minion_core.adapters.tg import chats_from
from minion_core.adapters.tg import spool_of
from minion_core.adapters.tg import spooled_or_dropped
from minion_core.kernel import ArchiveTo
from minion_core.kernel import DisposeSource
from minion_core.kernel import Folder
from minion_core.kernel import FolderSpec
from minion_core.kernel import Null
from minion_core.kernel import Reply
from minion_core.kernel import RouteOrigin
from minion_core.kernel import SeenPaths
from minion_core.kernel import SendResult

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Mapping
    from pathlib import Path

    from minion_core.kernel import Origin
    from minion_core.kernel import Sink
    from minion_core.kernel import Source
    from minion_core.kernel import Stage
    from minion_core.kernel import Step
    from minion_core.settings import Settings

Node: TypeAlias = dict[str, Any]
"""One parsed spec node: a kind key (source/step/sink) plus its knobs."""


class BadGraph(ValueError):
    """A spec the loader cannot assemble (loud, like BadConfig)."""


@dataclass(frozen=True)
class BuildContext:
    """The shared state a bot's nodes build against (one api/channel)."""

    cfg: Settings
    env: Mapping[str, str]
    bot: str
    api: TgApi
    channel: TgChannel


def context(cfg: Settings, env: Mapping[str, str], bot: str) -> BuildContext:
    """Build the shared context: one api and one channel per bot."""
    api = TgApi(env.get('TG_TOKEN', ''))
    return BuildContext(
        cfg=cfg, env=env, bot=bot, api=api, channel=TgChannel(api)
    )


def _dir(ctx: BuildContext, token: str) -> Path:
    """Resolve a directory alias to a real path (the only divergence)."""
    cfg, bot = ctx.cfg, ctx.bot
    table = {
        'inbox': cfg.inbox,
        'spool': cfg.bot_dir(bot) / '_spool',
        'bot_dir': cfg.bot_dir(bot),
        'bot_done': cfg.bot_done(bot),
        'print_queue': cfg.print_queue,
        'print_done': cfg.print_done,
    }
    if token not in table:
        raise BadGraph(f'unknown dir alias: {token}')
    return table[token]


LOCATORS: dict[str, Callable[[Origin], Path | None]] = {
    'spool': spool_of,
    'spooled-or-dropped': spooled_or_dropped,
}
"""Disposable-input locators a disposal sink may name."""


def _folder(ctx: BuildContext, node: Node) -> Source:
    """A watched-folder dock (offline; the project's default trigger)."""
    spec = FolderSpec(
        root=_dir(ctx, node.get('root', 'bot_dir')),
        dest=_dir(ctx, node.get('into', 'bot_dir')),
        exts=tuple(node['exts']),
        poll_sec=ctx.cfg.poll_sec,
        once=node.get('once', False),
    )
    return Folder(spec, SeenPaths(ctx.cfg.seen_paths_max))


def _tg(kind: Callable[[TgApi, TgSpec], Source]) -> Callable[..., Source]:
    """A Telegram dock factory over the given source class."""

    def make(ctx: BuildContext, node: Node) -> Source:
        spec = TgSpec(
            spool=SpoolSpec(
                into=_dir(ctx, node.get('into', 'bot_dir')),
                budget=functools.partial(free_quota, ctx.cfg),
            ),
            dest=_dir(ctx, node.get('dest', 'inbox')),
            offset=ctx.cfg.state / f'{ctx.bot}.offset',
            chats=chats_from(ctx.env),
            help=node.get('help', ''),
        )
        return kind(ctx.api, spec)

    return make


SOURCES: dict[str, Callable[[BuildContext, Node], Source]] = {
    'folder': _folder,
    'tg-media': _tg(TgMedia),
    'tg-any': _tg(TgAny),
    'tg-links': _tg(TgLinks),
}
"""Every dock the loader can name."""


def _reply(ctx: BuildContext, _node: Node) -> Sink:
    """Send the verdict's reply text back through the channel."""
    return Reply(ctx.channel)


def _send_result(ctx: BuildContext, _node: Node) -> Sink:
    """Send the delivered result file(s) through the channel."""
    return SendResult(ctx.channel)


def _null(_ctx: BuildContext, _node: Node) -> Sink:
    """A sink that does nothing (the unused side of a route)."""
    return Null()


def _archive(ctx: BuildContext, node: Node) -> Sink:
    """Move a delivered result into an archive directory."""
    return ArchiveTo(_dir(ctx, node['into']))


def _dispose(_ctx: BuildContext, node: Node) -> Sink:
    """Remove the consumed source after a decided delivery."""
    locate = LOCATORS.get(node.get('locate', ''))
    if locate is None:
        return DisposeSource()
    return DisposeSource(locate)


def _shelve(ctx: BuildContext, node: Node) -> Sink:
    """File the result and its original into one dated folder."""
    into = _dir(ctx, node.get('into', 'bot_done'))
    locate = LOCATORS.get(node.get('locate', ''))
    by_result = node.get('by_result', False)
    if locate is None:
        return Shelve(into, by_result=by_result)
    return Shelve(into, locate, by_result=by_result)


def _route(ctx: BuildContext, node: Node) -> Sink:
    """Delegate to one sink by the job's origin (tg vs loc)."""
    return RouteOrigin(
        tg=_subsink(ctx, node['tg']), loc=_subsink(ctx, node['loc'])
    )


SINKS: dict[str, Callable[[BuildContext, Node], Sink]] = {
    'reply': _reply,
    'send-result': _send_result,
    'null': _null,
    'archive': _archive,
    'dispose-source': _dispose,
    'shelve': _shelve,
    'route-origin': _route,
}
"""Every tail the loader can name."""


def _subsink(ctx: BuildContext, node: Node) -> Sink:
    """Resolve a nested sink node (route-origin's two sides)."""
    return SINKS[node['sink']](ctx, node)


StepCatalog: TypeAlias = 'Mapping[str, Callable[[Settings], Step]]'
"""Name -> Step factory: the Phase 0 catalog, injected by the caller."""


def _node(ctx: BuildContext, node: Node, steps: StepCatalog) -> Stage:
    """Resolve one node dict to its Stage by kind."""
    if 'source' in node:
        return SOURCES[node['source']](ctx, node)
    if 'step' in node:
        return steps[node['step']](ctx.cfg)
    if 'sink' in node:
        return SINKS[node['sink']](ctx, node)
    raise BadGraph(f'unknown node: {node}')


def _merge(docks: list[Stage]) -> Stage:
    """Fold docks into one belt with | (two docks, one belt)."""
    belt = docks[0]
    for dock in docks[1:]:
        belt = belt | dock
    return belt


def _stage(ctx: BuildContext, stage: Node, steps: StepCatalog) -> Stage:
    """One belt segment: a single node, or a merge of docks."""
    if 'merge' in stage:
        return _merge([_node(ctx, n, steps) for n in stage['merge']])
    return _node(ctx, stage, steps)


def _chain(stages: list[Stage]) -> Stage:
    """Fold stages left-to-right with >> (the belt order)."""
    belt = stages[0]
    for stage in stages[1:]:
        belt = belt >> stage
    return belt


def load(spec: Node, ctx: BuildContext, steps: StepCatalog) -> Stage:
    """Assemble the Stage a spec describes (graph as data)."""
    stages = [_stage(ctx, s, steps) for s in spec['stages']]
    if not stages:
        raise BadGraph('empty graph')
    return _chain(stages)
