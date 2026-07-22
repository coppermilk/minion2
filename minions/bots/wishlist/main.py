"""wishlist bot: a daily snapshot of a public wishlist spots changes.

Cadence belongs to cron (BLUEPRINT 11): this bot does one scan-act-exit
run and never reads the wall clock. Each run fetches the list (across
lek pages) and compares it to yesterday's snapshot (the database, in
STATE): every item gone since yesterday is a gift (photo + a Russian
thank-you), every item newly added is announced with a random blurb --
both weaving in the owner's own note when there is one.

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
import random
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from importlib import resources
from typing import TYPE_CHECKING
from typing import cast

from minion_core.adapters.admin import admin_config
from minion_core.adapters.files import BatchLock
from minion_core.adapters.schedule import cron_due
from minion_core.adapters.tg import TgApi
from minion_core.adapters.wishlist import SnapshotStore
from minion_core.adapters.wishlist import TgPhoto
from minion_core.adapters.wishlist import added
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


def load_messages() -> dict[str, object]:
    """The Russian templates (strings and lists), UTF-8 package data."""
    pkg = resources.files(__package__)
    raw = (pkg / 'messages.json').read_text(encoding='utf-8')
    return cast('dict[str, object]', json.loads(raw))


def _tmpl(templates: Mapping[str, object], key: str) -> str:
    """One string template by key, or '' when missing or not a string."""
    value = templates.get(key, '')
    return value if isinstance(value, str) else ''


def _form(entry: object, note: str) -> str:
    """A template's note-aware form: with-note when a note exists."""
    if not isinstance(entry, dict):
        return ''
    value = entry.get('with_note' if note else 'no_note')
    return value if isinstance(value, str) else ''


def _entries(templates: Mapping[str, object], key: str) -> list[object]:
    """The template objects stored as a list under ``key`` (any count)."""
    value = templates.get(key)
    return value if isinstance(value, list) else []


def _short(title: str) -> str:
    """A card title trimmed to its first ``TITLE_WORDS`` words."""
    return ' '.join(title.split()[:TITLE_WORDS])


def render(templates: Mapping[str, object], item: WishItem, link: str) -> str:
    """The thank-you caption for one gifted item (note-aware form)."""
    form = _form(templates.get('gift'), item.note)
    return form.format(item=_short(item.title), note=item.note, link=link)


def render_added(
    templates: Mapping[str, object], item: WishItem, link: str
) -> str:
    """Announce a newly added item, one template picked at random.

    ``added`` is a JSON list of note-aware forms, so its count is free to
    grow: a new phrasing is one more array entry and nothing here changes.
    """
    entries = _entries(templates, 'added')
    if not entries:
        return ''
    form = _form(random.choice(entries), item.note)  # noqa: S311 -- cosmetic
    return form.format(item=_short(item.title), note=item.note, link=link)


def digest(
    templates: Mapping[str, object], items: list[WishItem], link: str
) -> str:
    """A one-message summary of the current wishlist for the chat."""
    lines = [
        _tmpl(templates, 'digest_line').format(title=_short(i.title))
        for i in items[:DIGEST_MAX]
    ]
    head = _tmpl(templates, 'digest_head').format(count=len(items))
    return head + '\n' + '\n'.join(lines) + '\n\n' + link


@dataclass(frozen=True)
class _Report:
    """The chat side of one run: gift/add alerts and the digest."""

    deps: Deps
    templates: Mapping[str, object]
    link: str

    def gifts(self, previous: list[WishItem], current: list[WishItem]) -> None:
        """Post one photo+thank-you per item gone since yesterday."""
        for item in gifted(previous, current):
            caption = render(self.templates, item, self.link)
            self.deps.post(item.image, caption)
            _LOG.info('gifted id=%s title=%s', item.ident, item.title)

    def additions(
        self, previous: list[WishItem], current: list[WishItem]
    ) -> None:
        """Post one photo+random blurb per item added since yesterday."""
        for item in added(previous, current):
            caption = render_added(self.templates, item, self.link)
            self.deps.post(item.image, caption)
            _LOG.info('added id=%s title=%s', item.ident, item.title)

    def changes(
        self, previous: list[WishItem], current: list[WishItem]
    ) -> None:
        """Announce both what left (gifts) and what arrived (new wants)."""
        self.gifts(previous, current)
        self.additions(previous, current)

    def announce(self, items: list[WishItem]) -> None:
        """Send the current wishlist digest to the chat."""
        self.deps.say(digest(self.templates, items, self.link))


def run_once(cfg: Settings, spec: Spec, deps: Deps) -> None:
    """One scan: digest on first run, then announce gifts and additions."""
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
    if not first:
        report.changes(previous, current)
    store.save(current)
    _LOG.info('scanned prev=%d cur=%d', len(previous), len(current))


def build(cfg: Settings, env: Mapping[str, str]) -> tuple[Spec, Deps]:
    """Wire fetch and sender; url, chat and toggles are runtime admin knobs."""
    admin = admin_config(cfg.state)
    spec = Spec(
        url=admin.effective('wishlist_url', env.get('WISHLIST_URL', '')),
        chat=admin.effective('wishlist_chat', env.get('WISHLIST_CHAT', '')),
        announce=admin.get('wishlist_announce') == '1',
    )
    photo = TgPhoto(TgApi(env.get('TG_TOKEN', '')), spec.chat)
    return spec, Deps(fetch=fetch_items, post=photo.post, say=photo.text)


def main(env: Mapping[str, str] | None = None) -> int:
    """One overlap-safe run; unconfigured or admin-disabled, a clean no-op."""
    mapping = os.environ if env is None else env
    cfg = load(mapping)
    log = bot_logger(BOT, cfg.logs)
    admin = admin_config(cfg.state)
    if admin.get('wishlist_enabled') == '0':
        log.info('skipped reason=disabled_by_admin')
        return 0
    if not cron_due(admin.get('wishlist_cron'), datetime.now(tz=UTC)):
        log.info('skipped reason=not_scheduled')
        return 0
    lock = BatchLock(cfg.state / f'{BOT}.lock')
    if not lock.acquire():
        log.warning('skipped reason=batch_locked')
        return 0
    try:
        spec, deps = build(cfg, mapping)
        run_once(cfg, spec, deps)
    finally:
        lock.release()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
