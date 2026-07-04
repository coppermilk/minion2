"""fan-save bot: a link becomes a video parked for later work.

Graph: TgLinks -> FetchLink -> Reply -> Shelve. Drop a
TikTok/YouTube/anything link in the chat and the video lands in the
fan queue under ``done/<MMDD> <title>/`` with the link spool filed in
its ``_done/`` -- content collected now, processed separately later.
The chat gets a text confirmation only; the file itself never goes
back.

Part of the one-identity-per-behaviour waiver (BLUEPRINT 9): the
graph shape matches fetch with ``FETCH_SINK=queue``, but it runs as
its own Telegram bot with its own offset and work dir.
"""

from __future__ import annotations

import functools
import os
from typing import TYPE_CHECKING

from minion_core.adapters.fetch import FetchLink
from minion_core.adapters.files import Shelve
from minion_core.adapters.files import free_quota
from minion_core.adapters.tg import SpoolSpec
from minion_core.adapters.tg import TgApi
from minion_core.adapters.tg import TgChannel
from minion_core.adapters.tg import TgLinks
from minion_core.adapters.tg import TgSpec
from minion_core.adapters.tg import chats_from
from minion_core.adapters.tg import spool_of
from minion_core.kernel import Reply
from minion_core.kernel import run
from minion_core.settings import load

if TYPE_CHECKING:
    from collections.abc import Mapping

    from minion_core.kernel import Stage
    from minion_core.settings import Settings

BOT = 'fan-save'


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
        help='Send a link and I save the video to your fan queue.',
    )
    channel = TgChannel(api)
    return (
        TgLinks(api, spec)
        >> FetchLink(cfg)
        >> Reply(channel)
        >> Shelve(cfg.bot_done(BOT), spool_of, by_result=True)
    )


def main(env: Mapping[str, str] | None = None) -> int:
    """Build Settings once and drain the belt."""
    mapping = os.environ if env is None else env
    cfg = load(mapping)
    return run(BOT, build(cfg, mapping), cfg.logs)


if __name__ == '__main__':
    raise SystemExit(main())
