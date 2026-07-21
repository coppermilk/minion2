"""wishlist bot: a daily snapshot of a public wishlist spots gifts.

Cadence belongs to cron (BLUEPRINT 11): this bot does one scan-act-exit
run and never reads the wall clock. Each run fetches the list, compares
it to yesterday's snapshot (the database, in STATE), and for every item
that vanished posts its photo and a Russian thank-you to the chat, then
saves today's list as the new snapshot.

A failed or blocked fetch keeps the old snapshot untouched and posts
nothing (REQ-DEG-001 spirit): a scrape that returns nothing is a block,
not a hundred gifts. Overlap-safe via a batch lock (REQ-RES-003). The
Russian template lives in ``messages.json`` (UTF-8), so the repo-wide
ASCII law still holds for every ``.py``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING
from typing import cast

from minion_core.adapters.files import BatchLock
from minion_core.adapters.tg import TgApi
from minion_core.adapters.wishlist import SnapshotStore
from minion_core.adapters.wishlist import TgPhoto
from minion_core.adapters.wishlist import fetch_items
from minion_core.adapters.wishlist import gifted
from minion_core.kernel import bot_logger
from minion_core.settings import load

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Mapping

    from minion_core.adapters.wishlist import WishItem
    from minion_core.settings import Settings

BOT = 'wishlist'

_LOG = logging.getLogger(BOT)


@dataclass(frozen=True)
class Spec:
    """The list to watch and the chat that gets the thank-you."""

    url: str
    chat: str


@dataclass(frozen=True)
class Deps:
    """Injected boundaries, so the run is testable without a network."""

    fetch: Callable[[str], list[WishItem] | None]
    post: Callable[[str, str], None]


def load_messages() -> dict[str, str]:
    """The Russian gift template, stored as UTF-8 package data."""
    pkg = resources.files(__package__)
    raw = (pkg / 'messages.json').read_text(encoding='utf-8')
    return cast('dict[str, str]', json.loads(raw))


def render(templates: Mapping[str, str], item: WishItem, link: str) -> str:
    """The thank-you caption for one gifted item."""
    return templates['gift'].format(item=item.title, link=link)


def run_once(cfg: Settings, spec: Spec, deps: Deps) -> None:
    """One scan: announce every vanished item, then re-snapshot."""
    if not spec.url or not spec.chat:
        _LOG.info('idle: WISHLIST_URL or WISHLIST_CHAT not set')
        return
    current = deps.fetch(spec.url)
    if current is None:
        _LOG.warning('fetch_failed: snapshot kept, nothing posted')
        return
    store = SnapshotStore(cfg.state / f'{BOT}.json')
    previous = store.load()
    templates = load_messages()
    for item in gifted(previous, current):
        deps.post(item.image, render(templates, item, spec.url))
        _LOG.info('gifted id=%s title=%s', item.ident, item.title)
    store.save(current)
    _LOG.info('scanned prev=%d cur=%d', len(previous), len(current))


def build(env: Mapping[str, str]) -> tuple[Spec, Deps]:
    """Wire the real fetch and Telegram sender from the environment."""
    spec = Spec(
        url=env.get('WISHLIST_URL', ''),
        chat=env.get('WISHLIST_CHAT', ''),
    )
    photo = TgPhoto(TgApi(env.get('TG_TOKEN', '')), spec.chat)
    return spec, Deps(fetch=fetch_items, post=photo.post)


def main(env: Mapping[str, str] | None = None) -> int:
    """One overlap-safe run; unconfigured, a clean no-op (exit 0)."""
    mapping = os.environ if env is None else env
    cfg = load(mapping)
    log = bot_logger(BOT, cfg.logs)
    lock = BatchLock(cfg.state / f'{BOT}.lock')
    if not lock.acquire():
        log.warning('skipped reason=batch_locked')
        return 0
    try:
        spec, deps = build(mapping)
        run_once(cfg, spec, deps)
    finally:
        lock.release()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
