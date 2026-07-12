"""relay bot: a thin Telegram transport in front of an atomic service.

Graph: (TgMedia | Folder) -> CallService -> RouteOrigin(chat / nothing) ->
Reply -> Shelve. The heavy transform (blur, frames, ...) lives in a
separate service container; this container only receives a document over
Telegram (or a folder drop), POSTs it to ``SERVICE_URL/run-file``, and
sends the bytes back -- no model, no torch. One generic module, N
containers: ``SERVICE_URL`` picks the service and ``RELAY_NAME`` the work
dir / offset, so ``tg-censor-blur`` and ``tg-frames`` are the same image
with different env (the Telegram <-> service split).
"""

from __future__ import annotations

import functools
import os
from typing import TYPE_CHECKING

from minion_core.adapters.files import Shelve
from minion_core.adapters.files import free_quota
from minion_core.adapters.service_call import CallService
from minion_core.adapters.service_call import ServiceCall
from minion_core.adapters.tg import SpoolSpec
from minion_core.adapters.tg import TgAny
from minion_core.adapters.tg import TgApi
from minion_core.adapters.tg import TgChannel
from minion_core.adapters.tg import TgLinks
from minion_core.adapters.tg import TgMedia
from minion_core.adapters.tg import TgSpec
from minion_core.adapters.tg import chats_from
from minion_core.adapters.tg import spooled_or_dropped
from minion_core.kernel import FolderSpec
from minion_core.kernel import Null
from minion_core.kernel import Reply
from minion_core.kernel import RouteOrigin
from minion_core.kernel import SeenPaths
from minion_core.kernel import SendResult
from minion_core.kernel import merge_watch
from minion_core.kernel import run
from minion_core.settings import load

if TYPE_CHECKING:
    from collections.abc import Mapping

    from minion_core.kernel import Source
    from minion_core.kernel import Stage
    from minion_core.settings import Settings

_DOCKS = {'media': TgMedia, 'any': TgAny, 'links': TgLinks}
"""RELAY_DOCK -> the Telegram dock: documents, links+documents, or links."""

_DEFAULT_EXTS = (
    '.jpg',
    '.jpeg',
    '.png',
    '.webp',
    '.mp4',
    '.mkv',
    '.webm',
    '.mov',
    '.avi',
)
"""Media suffixes the folder drop accepts by default (RELAY_EXTS overrides)."""

_DEFAULT_HELP = 'Send or drop a file and I run it through the service.'

_ACKS = {
    'censor-blur': 'Got it -- blurring the people in your photo...',
    'censor-black': 'Got it -- blacking out the faces...',
    'restore': 'Got it -- erasing the people and repainting the scene...',
    'frames': 'Got it -- extracting the frames, back in a moment...',
    'fetch': 'Got it -- fetching the video...',
    'fan-save': 'Got it -- saving the video to your queue...',
}
"""Per-bot acknowledgement, sent the moment a message is seen (before the
download). Keyed by RELAY_NAME; an unlisted bot falls back to a generic ack."""

_DEFAULT_ACK = 'Got it -- working on it...'


def _name(env: Mapping[str, str]) -> str:
    """The relay's identity: its work dir, offset, and done folder."""
    return env.get('RELAY_NAME', 'relay')


def _exts(env: Mapping[str, str]) -> tuple[str, ...]:
    """The watched suffixes: RELAY_EXTS (csv) or the media default."""
    raw = env.get('RELAY_EXTS', '')
    if not raw.strip():
        return _DEFAULT_EXTS
    parts = (p.strip().lower() for p in raw.split(','))
    return tuple(p if p.startswith('.') else f'.{p}' for p in parts if p)


def _dock(env: Mapping[str, str], api: TgApi, spec: TgSpec) -> Source:
    """The Telegram dock chosen by RELAY_DOCK (media | any | links)."""
    make = _DOCKS.get(env.get('RELAY_DOCK', 'media'), TgMedia)
    return make(api, spec)


def build(cfg: Settings, env: Mapping[str, str]) -> Stage:
    """Assemble the relay belt; secrets come from the passed mapping."""
    api = TgApi(env.get('TG_TOKEN', ''))
    name = _name(env)
    spool = cfg.bot_dir(name) / '_spool'
    spec = TgSpec(
        spool=SpoolSpec(into=spool, budget=functools.partial(free_quota, cfg)),
        dest=spool,
        offset=cfg.state / f'{name}.offset',
        chats=chats_from(env),
        help=env.get('RELAY_HELP', _DEFAULT_HELP),
        ack=_ACKS.get(name, _DEFAULT_ACK),
    )
    channel = TgChannel(api)
    watch = FolderSpec(
        root=cfg.bot_dir(name),
        dest=spool,
        exts=_exts(env),
        poll_sec=cfg.poll_sec,
    )
    seen = SeenPaths(cfg.seen_paths_max)
    docks = merge_watch(_dock(env, api, spec), watch, seen)
    call = CallService(ServiceCall(env.get('SERVICE_URL', '')))
    route = RouteOrigin(tg=SendResult(channel), loc=Null())
    return (
        docks
        >> call
        >> route
        >> Reply(channel)
        >> Shelve(cfg.bot_done(name), spooled_or_dropped)
    )


def main(env: Mapping[str, str] | None = None) -> int:
    """Build Settings once and drain the relay belt."""
    mapping = os.environ if env is None else env
    cfg = load(mapping)
    (cfg.bot_dir(_name(mapping)) / '_spool').mkdir(parents=True, exist_ok=True)
    return run(_name(mapping), build(cfg, mapping), cfg.logs)


if __name__ == '__main__':
    raise SystemExit(main())
