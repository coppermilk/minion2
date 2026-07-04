"""fetch bot: a link in chat becomes a video file.

Graph: TgLinks -> FetchLink -> (SendResult | ArchiveTo) -> Reply ->
DisposeSource. The sink is the bot's config axis (BLUEPRINT 9):
``FETCH_SINK=chat`` sends the file back, ``queue`` parks it in the
fan queue.
"""

from __future__ import annotations

import functools
import os
from typing import TYPE_CHECKING

from minion_core.adapters.fetch import FetchLink
from minion_core.adapters.files import free_quota
from minion_core.adapters.tg import SpoolSpec
from minion_core.adapters.tg import TgApi
from minion_core.adapters.tg import TgChannel
from minion_core.adapters.tg import TgLinks
from minion_core.adapters.tg import TgSpec
from minion_core.adapters.tg import chats_from
from minion_core.adapters.tg import spool_of
from minion_core.kernel import ArchiveTo
from minion_core.kernel import DisposeSource
from minion_core.kernel import Reply
from minion_core.kernel import SendResult
from minion_core.kernel import run
from minion_core.settings import CHAT
from minion_core.settings import load

if TYPE_CHECKING:
    from collections.abc import Mapping

    from minion_core.kernel import Sink
    from minion_core.kernel import Stage
    from minion_core.settings import Settings

BOT = 'fetch'


def _sink(cfg: Settings, channel: TgChannel) -> Sink:
    """The config axis: chat sends back, queue parks the file."""
    if cfg.fetch_sink == CHAT:
        return SendResult(channel)
    return ArchiveTo(cfg.bot_done(BOT))


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
        help='Send a link and I fetch the video.',
    )
    channel = TgChannel(api)
    return (
        TgLinks(api, spec)
        >> FetchLink(cfg)
        >> _sink(cfg, channel)
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
