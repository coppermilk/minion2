"""wishlist bot: a daily snapshot of a public wishlist spots gifts.

Cadence belongs to cron (BLUEPRINT 11): this bot does one scan-act-exit
run and never reads the wall clock. Each run fetches the list (across
lek pages), compares it to yesterday's snapshot (the database, in
STATE), and for every item that vanished posts its photo and a Russian
thank-you -- weaving in the owner's own note -- to the chat, then saves
today's list as the new snapshot.

The first run (no snapshot yet) posts a digest of the current wishlist
so the operator sees what is being watched; ``WISHLIST_ANNOUNCE=1``
forces that digest on any run. A failed or blocked fetch keeps the old
snapshot and posts nothing (REQ-DEG-001 spirit). Overlap-safe via a
batch lock (REQ-RES-003). The Russian templates live in
``messages.json`` (UTF-8), so the repo-wide ASCII law holds for every
``.py``.
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

TITLE_WORDS = 5
"""Long card names are trimmed to their first few words."""

DIGEST_MAX = 80
"""Cap on items listed in one wishlist digest (bounded message)."""

_LOG = logging.getLogger(BOT)


@dataclass(frozen=True)
class Spec:
    """The list to watch, the chat, and whether to force a digest."""

    url: str
    chat: str
    announce: bool = False


@dataclass(frozen=True)
class Deps:
    """Injected boundaries, so the run is testable without a network."""

    fetch: Callable[[str], list[WishItem] | None]
    post: Callable[[str, str], None]
    say: Callable[[str], None]


def load_messages() -> dict[str, str]:
    """The Russian templates, stored as UTF-8 package data."""
    pkg = resources.files(__package__)
    raw = (pkg / 'messages.json').read_text(encoding='utf-8')
    return cast('dict[str, str]', json.loads(raw))


def _short(title: str) -> str:
    """A card title trimmed to its first ``TITLE_WORDS`` words."""
    return ' '.join(title.split()[:TITLE_WORDS])


def render(templates: Mapping[str, str], item: WishItem, link: str) -> str:
    """The thank-you caption for one gifted item, note woven in."""
    note = templates['note_line'].format(note=item.note) if item.note else ''
    return templates['gift'].format(
        item=_short(item.title), note=note, link=link
    )


def digest(
    templates: Mapping[str, str], items: list[WishItem], link: str
) -> str:
    """A one-message summary of the current wishlist for the chat."""
    lines = [
        templates['digest_line'].format(title=_short(i.title))
        for i in items[:DIGEST_MAX]
    ]
    head = templates['digest_head'].format(count=len(items))
    return head + '\n' + '\n'.join(lines) + '\n\n' + link


@dataclass(frozen=True)
class _Report:
    """The chat side of one run: gift alerts and the digest."""

    deps: Deps
    templates: Mapping[str, str]
    link: str

    def gifts(self, previous: list[WishItem], current: list[WishItem]) -> None:
        """Post one photo+thank-you per item gone since yesterday."""
        for item in gifted(previous, current):
            caption = render(self.templates, item, self.link)
            self.deps.post(item.image, caption)
            _LOG.info('gifted id=%s title=%s', item.ident, item.title)

    def announce(self, items: list[WishItem]) -> None:
        """Send the current wishlist digest to the chat."""
        self.deps.say(digest(self.templates, items, self.link))


def run_once(cfg: Settings, spec: Spec, deps: Deps) -> None:
    """One scan: digest on first run, announce vanished items, snapshot."""
    if not spec.url or not spec.chat:
        _LOG.info('idle: WISHLIST_URL or WISHLIST_CHAT not set')
        return
    current = deps.fetch(spec.url)
    if current is None:
        _LOG.warning('fetch_failed: snapshot kept, nothing posted')
        return
    store = SnapshotStore(cfg.state / f'{BOT}.json')
    first = not store.path.exists()
    previous = store.load()
    report = _Report(deps, load_messages(), spec.url)
    if first or spec.announce:
        report.announce(current)
    report.gifts(previous, current)
    store.save(current)
    _LOG.info('scanned prev=%d cur=%d', len(previous), len(current))


def build(env: Mapping[str, str]) -> tuple[Spec, Deps]:
    """Wire the real fetch and Telegram sender from the environment."""
    spec = Spec(
        url=env.get('WISHLIST_URL', ''),
        chat=env.get('WISHLIST_CHAT', ''),
        announce=env.get('WISHLIST_ANNOUNCE', '') == '1',
    )
    photo = TgPhoto(TgApi(env.get('TG_TOKEN', '')), spec.chat)
    return spec, Deps(fetch=fetch_items, post=photo.post, say=photo.text)


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
