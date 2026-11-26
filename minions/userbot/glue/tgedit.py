# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Apply the channel-edit packages of the site's feed.

The Telethon side of ``core/tgedit``. The packages live in the site's
feed (``engines.tgedit.feed_url``, ``/tgedit.json``); merging a package
into the site's ``main`` IS the approval, so the worker asks nobody. Each
tick, inside the persona's waking hours, it reads the feed -- unreachable,
unparsable or of another version: one log line, nothing done, the next
tick tries again -- and for each package not handled yet it:

1. checks the account's rights in the channel and the package's limits
   (the stricter of the feed's ``rules`` and our own);
2. reads every post the package names and DRY-RUNS each item against the
   post as it is now (``old`` must match it character for character);
3. ``dry`` mode stops there; ``on`` mode journals every post it is about
   to change (text and entities, for an undo by hand) and applies what
   passed, one write at least ``pause_sec`` (never under 10 s) after
   another.

Either way every item's outcome -- ``post -> applied / skipped (reason) /
error`` -- goes to the container log and to the journal.

A new text may carry premium emoji (``<tg-emoji emoji-id=...>``); only ids
of the catalog pass. The catalog -- the account's installed custom-emoji
sets, every custom emoji in the channel's posts and the constants'
``emoji`` -- is rebuilt once a day, logged (one line, plus one per set) and
written to ``emoji_catalog.json`` next to ``tgedit.json``, so a package
author can pick an id from it. ``mode`` lives in
``engines.tgedit``: switching it is a reviewed change of the constants
file, not a chat command. A handled package is remembered by id in
``tgedit.json``, so it is applied once; a post the package does not name
is never read, let alone written; a package the feed no longer carries
is not applied.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from typing import TYPE_CHECKING
from typing import cast

from minion_core.adapters import feed as feeds
from minion_core.adapters import userchat
from minion_core.kernel import atomic_write
from minions.userbot.core import codec
from minions.userbot.core import relink
from minions.userbot.core import tgedit

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from collections.abc import Mapping
    from pathlib import Path

log = logging.getLogger('userbot')

STATE_FILE = 'tgedit.json'
JOURNAL_FILE = 'tgedit_journal.jsonl'
CATALOG_FILE = 'emoji_catalog.json'
REPORT_FILE = 'tgedit_report.json'
"""Every package's outcomes for the site (``tgedit.summary``); nas-update
relays it to the site's version reports when it changes. Kept until the NAS
sends the outbox copy below; then this file and its relay go."""
OUTBOX_DIR = 'outbox'
"""The shared NAS -> site channel: nas-update sends every ``<kind>.json``
here to the site when its meaning changes, so a new kind needs no NAS
config."""
OUTBOX_REPORT = 'tgedit-report.json'
"""The report's kind on that channel (the site's ``INBOX_KINDS``)."""
CATALOG_SEC = 24 * 3600.0
"""How often the emoji catalog is rebuilt (a failed read retries sooner)."""
CATALOG_POSTS = 200
"""Newest channel posts whose custom emoji join the catalog."""
SET, CHANNEL, CONSTANTS = 'set', 'channel', 'constants'
"""Where a catalog emoji was seen."""
FEED_URL = 'https://daetotyoma.com/tgedit.json'
"""The site's feed of reviewed packages (``engines.tgedit.feed_url``)."""
MODES = ('off', 'dry', 'on')
RECENT = 20
"""Newest posts read to tell whether a pin_new post is already out."""
NEEDS = {
    tgedit.EDIT: 'edit',
    tgedit.LINK: 'edit',
    tgedit.DELETE: 'delete',
    tgedit.DESC: 'info',
    tgedit.PIN_NEW: 'pin',
}
"""The channel right (``userchat.Account.rights``) each action needs."""


@dataclass(frozen=True)
class EditParams:
    """The worker's knobs, from ``engines.tgedit`` and ``persona``."""

    mode: str = 'off'
    feed_url: str = FEED_URL
    max_deletes: int = tgedit.MAX_DELETES
    pause_sec: float = tgedit.MIN_PAUSE_SEC
    tick_sec: float = 900.0
    guard: tgedit.Guard = field(default_factory=tgedit.Guard)
    labels: dict[str, str] = field(default_factory=dict)
    clock: relink.Clock = field(default_factory=relink.Clock)
    emoji: tuple[tuple[str, str, str], ...] = ()
    """The constants' premium emoji: (type, id, fallback glyph)."""


def _words(value: object) -> tuple[str, ...]:
    """Read a JSON list of strings, dropping anything else."""
    return tuple(str(v) for v in codec.rows(value) if isinstance(v, str))


def _constant_emoji(
    settings: Mapping[str, object],
) -> tuple[tuple[str, str, str], ...]:
    """Read the constants' ``emoji`` list: (type, id, glyph) with an id."""
    out: list[tuple[str, str, str]] = []
    for row in codec.rows(settings.get('emoji')):
        if not isinstance(row, dict):
            continue
        ident = codec.text(row.get('id'), '')
        if ident:
            kind = codec.text(row.get('type'), '')
            out.append((kind, ident, codec.text(row.get('fallback'), '')))
    return tuple(out)


def load_edit_params(settings: Mapping[str, object]) -> EditParams:
    """Read ``engines.tgedit``; an unknown mode reads as ``off``.

    The two hard limits are clamped here, so no constants file can raise
    the deletions cap above 15 or bring two writes closer than 10 s. The
    constants' premium emoji are the catalog before the account is read.
    """
    cfg = codec.engine(settings, 'tgedit')
    persona = codec.section(settings, 'persona')
    mode = codec.text(cfg.get('mode'), 'off')
    labels = codec.table(cfg.get('labels'))
    emoji = _constant_emoji(settings)
    return EditParams(
        mode=mode if mode in MODES else 'off',
        feed_url=codec.text(cfg.get('feed_url'), FEED_URL),
        max_deletes=min(
            tgedit.MAX_DELETES,
            int(codec.num(cfg.get('max_deletes'), tgedit.MAX_DELETES)),
        ),
        pause_sec=max(
            tgedit.MIN_PAUSE_SEC,
            codec.num(cfg.get('pause_sec'), tgedit.MIN_PAUSE_SEC),
        ),
        tick_sec=codec.num(cfg.get('tick_sec'), 900.0),
        guard=tgedit.Guard(
            urls=_words(cfg.get('protect_urls')),
            link_texts=_words(cfg.get('protect_link_texts')),
            emoji=frozenset(ident for _, ident, _ in emoji),
        ),
        labels={str(k): str(v) for k, v in labels.items()},
        clock=relink.Clock(
            tz_offset_hours=codec.num(persona.get('tz_offset_hours'), 0.0),
            wake_start_hour=int(codec.num(persona.get('wake_start_hour'), 0)),
            wake_end_hour=int(codec.num(persona.get('wake_end_hour'), 24)),
        ),
        emoji=emoji,
    )


@dataclass(frozen=True)
class EditDeps:
    """Everything the worker may reach; nothing else is in scope."""

    account: userchat.Account
    channel: Callable[[], int]  # the profile's channel (test or live)
    feed: Callable[[], Awaitable[str]]  # the feed's text (read_feed)
    path: Path  # tgedit.json
    journal: Path  # tgedit_journal.jsonl
    catalog: Path  # emoji_catalog.json
    params: EditParams
    report: Path | None = None  # tgedit_report.json; None writes none
    outbox: Path | None = None  # outbox/; None writes no copy there
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep


@dataclass
class EditState:
    """What survives a restart: package ids applied and dry-reported."""

    done: set[str] = field(default_factory=set)
    dry: set[str] = field(default_factory=set)


def read_state(path: Path) -> EditState:
    """Load ``tgedit.json``; an unreadable file starts empty."""
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return EditState()
    if not isinstance(raw, dict):
        return EditState()

    def ids(key: str) -> set[str]:
        return {v for v in codec.rows(raw.get(key)) if isinstance(v, str)}

    return EditState(done=ids('done'), dry=ids('dry'))


def write_state(path: Path, state: EditState) -> None:
    """Persist the state atomically."""
    body = {'done': sorted(state.done), 'dry': sorted(state.dry)}
    atomic_write(path, json.dumps(body, indent=2).encode('utf-8'))


def read_journal(path: Path) -> list[dict[str, object]]:
    """Load the journal's rows; a missing file or a torn line is skipped."""
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    rows: list[dict[str, object]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def to_post(linked: userchat.Linked) -> tgedit.Post:
    """Convert the adapter's post into the pure planner's."""
    return tgedit.Post(
        id=linked.id,
        text=linked.text,
        spans=linked.spans,
        kinds=linked.kinds or ('other',) * len(linked.spans),
        links=tuple((one.index, one.url) for one in linked.links),
        media=linked.media,
    )


def _shown(text: str, span: tuple[int, int]) -> str:
    """Return the text an entity's UTF-16 (offset, length) covers."""
    offset, length = span
    units = text.encode('utf-16-le')
    return units[offset * 2 : (offset + length) * 2].decode(
        'utf-16-le', errors='ignore'
    )


@dataclass
class _Catalog:
    """Catalog rows being merged, one per emoji id, in order first seen."""

    rows: dict[str, dict[str, object]] = field(default_factory=dict)

    def add(self, ident: str, glyph: str, source: str, **extra: str) -> None:
        """Record one sighting of an emoji; the first glyph and names win."""
        if not ident:
            return
        row = self.rows.setdefault(
            ident, {'id': ident, 'fallback': glyph, 'source': []}
        )
        seen = cast('list[str]', row['source'])
        if source not in seen:
            seen.append(source)
        for key, value in extra.items():
            if value:
                row.setdefault(key, value)

    def add_set(self, one: userchat.EmojiSet) -> None:
        """Record every emoji of an installed set."""
        for ident, glyph in one.emoji:
            self.add(ident, glyph, SET, set=one.short_name)

    def add_post(self, post: userchat.Linked) -> None:
        """Record every custom emoji of a channel post."""
        for ident, span in zip(post.refs, post.spans, strict=False):
            self.add(ident, _shown(post.text, span), CHANNEL)


def catalog_rows(
    sets: list[userchat.EmojiSet],
    posts: list[userchat.Linked],
    constants: tuple[tuple[str, str, str], ...],
) -> list[dict[str, object]]:
    """Merge every premium emoji seen into one row per id.

    A row: ``id``, ``fallback`` glyph, ``source`` (every place it was seen:
    ``set``, ``channel``, ``constants``), and ``set`` / ``type`` when known.
    """
    out = _Catalog()
    for one in sets:
        out.add_set(one)
    for post in posts:
        out.add_post(post)
    for kind, ident, glyph in constants:
        out.add(ident, glyph, CONSTANTS, type=kind)
    return list(out.rows.values())


def _log_catalog(
    heads: list[dict[str, object]],
    rows: list[dict[str, object]],
    path: Path,
) -> None:
    """Log the catalog: one line of counts, then one line per set."""
    by = {
        source: sum(
            1 for row in rows if source in cast('list[str]', row['source'])
        )
        for source in (SET, CHANNEL, CONSTANTS)
    }
    log.info(
        'tgedit: emoji catalog: %d sets, %d emoji '
        '(set %d, channel %d, constants %d) -> %s',
        len(heads),
        len(rows),
        by[SET],
        by[CHANNEL],
        by[CONSTANTS],
        path,
    )
    for head in heads:
        log.info(
            'tgedit: emoji set %s "%s": %d',
            head['short_name'],
            head['title'],
            head['count'],
        )


def read_feed(url: str) -> Callable[[], Awaitable[str]]:
    """Return the reader of the feed at ``url``, off the event loop."""

    async def read() -> str:
        return await asyncio.to_thread(feeds.read, url)

    return read


class ChannelUnreadableError(RuntimeError):
    """Telegram could not be read; the package waits for the next tick."""


@dataclass
class _Run:
    """One package's pass: outcomes so far and whether writes go out."""

    package: str
    live: bool
    pause_sec: float = tgedit.MIN_PAUSE_SEC
    outcomes: list[tgedit.Outcome] = field(default_factory=list)
    writes: int = 0


@dataclass
class ChannelEditor:
    """Apply the feed's packages to the channel."""

    deps: EditDeps
    state: EditState = field(default_factory=EditState)
    known: frozenset[str] = frozenset()
    """Premium-emoji ids of the last catalog (the constants' always pass)."""
    catalog_at: float = 0.0

    def load(self) -> None:
        """Read the persisted state; bring the site's report up to date."""
        self.state = read_state(self.deps.path)
        self.write_report(time.time())

    def write_report(self, now: float) -> None:
        """Rewrite ``tgedit_report.json`` from the whole journal.

        The journal is the record of what the channel got; the report is
        only its digest, so it is rebuilt rather than kept in step.
        """
        targets = [self.deps.report]
        if self.deps.outbox is not None:
            targets.append(self.deps.outbox / OUTBOX_REPORT)
        paths = [path for path in targets if path is not None]
        if not paths:
            return
        body = tgedit.summary(read_journal(self.deps.journal), now)
        data = json.dumps(body, ensure_ascii=False, indent=1).encode('utf-8')
        for path in paths:
            atomic_write(path, data)

    def save(self) -> None:
        """Write the state."""
        write_state(self.deps.path, self.state)

    async def loop(self) -> None:
        """Tick forever; one failed tick is logged and retried later."""
        while True:
            try:
                await self.tick(time.time())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('tgedit: tick failed; will retry')
            await asyncio.sleep(self.deps.params.tick_sec)

    async def tick(self, now: float) -> int:
        """Handle every pending package; return how many were handled."""
        params = self.deps.params
        if params.mode == 'off' or not params.clock.awake(now):
            return 0
        if now - self.catalog_at >= CATALOG_SEC:
            await self.catalog(now)
        try:
            feed = tgedit.parse_feed(await self.deps.feed())
        except (feeds.FeedUnreachableError, tgedit.FeedError) as exc:
            log.warning('tgedit: feed skipped this tick: %s', exc)
            return 0
        rules = feed.rules.tighten(params.max_deletes, params.pause_sec)
        pending = self._pending(feed.packages)
        for package in pending:
            await self._handle(package, rules)
        return len(pending)

    @property
    def guard(self) -> tgedit.Guard:
        """The guard, with the catalog's ids as the emoji a text may carry."""
        base = self.deps.params.guard
        return replace(base, emoji=base.emoji | self.known)

    async def catalog(self, now: float) -> None:
        """Rebuild the emoji catalog: log it, write ``emoji_catalog.json``.

        Unreadable sets keep the channel's and the constants' emoji and try
        again next tick; read, the catalog waits ``CATALOG_SEC``.
        """
        account = self.deps.account
        sets = await account.emoji_sets()
        posts = await account.linked(self.deps.channel(), CATALOG_POSTS)
        rows = catalog_rows(sets or [], posts, self.deps.params.emoji)
        self.known = frozenset(str(row['id']) for row in rows)
        heads = [
            {
                'short_name': one.short_name,
                'title': one.title,
                'count': len(one.emoji),
            }
            for one in sets or []
        ]
        body = {
            'generated_at': time.strftime(
                '%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)
            ),
            'sets_read': sets is not None,
            'sets': heads,
            'emoji': rows,
        }
        atomic_write(
            self.deps.catalog,
            json.dumps(body, ensure_ascii=False, indent=1).encode('utf-8'),
        )
        _log_catalog(heads, rows, self.deps.catalog)
        if sets is None:
            log.warning('tgedit: emoji sets unreadable; will retry next tick')
            return
        if not sets:
            log.info('tgedit: the account has no custom emoji sets installed')
        self.catalog_at = now

    def _pending(
        self, packages: tuple[tgedit.Package, ...]
    ) -> list[tgedit.Package]:
        """Return the feed's packages not handled in the current mode yet."""
        seen = self.state.dry if self.deps.params.mode == 'dry' else set()
        return [
            one for one in packages if one.id not in seen | self.state.done
        ]

    async def _handle(
        self, package: tgedit.Package, rules: tgedit.Rules
    ) -> None:
        """Dry-run (and in ``on`` mode apply) one package; report it."""
        live = self.deps.params.mode == 'on'
        run = _Run(package.id, live, rules.min_pause_sec)
        try:
            tgedit.check_limits(package.items, rules.max_deletes)
        except tgedit.PackageError as exc:
            self._report(run, refused=str(exc))
            return
        rights = await self.deps.account.rights(self.deps.channel())
        if rights is None:
            msg = f'tgedit: rights in the channel unreadable ({run.package})'
            raise ChannelUnreadableError(msg)
        await self._posts(run, package.items, rights)
        await self._channel(run, package.items, rights)
        self._report(run)

    async def _posts(
        self, run: _Run, items: tuple[tgedit.Item, ...], rights: frozenset[str]
    ) -> None:
        """Dry-run and apply the items on posts, one post at a time."""
        groups = tgedit.by_post(items)
        chat = self.deps.channel()
        read = await self.deps.account.posts(chat, list(groups))
        if read is None:
            msg = f'tgedit: posts unreadable ({run.package})'
            raise ChannelUnreadableError(msg)
        for post_id, group in groups.items():
            missing = {NEEDS[one.action] for one in group} - rights
            if missing:
                run.outcomes += _same(group, tgedit.NO_RIGHTS)
                continue
            found = read.get(post_id)
            plan = tgedit.plan_post(
                to_post(found) if found else None, group, self.guard
            )
            if not run.live or not plan.changes or found is None:
                run.outcomes += plan.outcomes
                continue
            landed = await self._write_post(run, found, plan)
            run.outcomes += _landed(plan.outcomes, landed=landed)

    async def _write_post(
        self, run: _Run, post: userchat.Linked, plan: tgedit.PostPlan
    ) -> bool:
        """Journal the post, then delete or edit it; True when it landed."""
        chat = self.deps.channel()
        await self._pace(run)
        self._journal(run.package, 'post', userchat.dump(post))
        if plan.delete:
            return await self.deps.account.delete(chat, post.id)
        edit = userchat.Relinked(plan.text, plan.spans, plan.urls, plan.emoji)
        return await self.deps.account.relink(chat, post, edit)

    async def _channel(
        self, run: _Run, items: tuple[tgedit.Item, ...], rights: frozenset[str]
    ) -> None:
        """Dry-run and apply the description and new-pin items."""
        for item in items:
            if item.action not in tgedit.CHANNEL_ACTIONS:
                continue
            needs = {NEEDS[item.action]} | (
                {'post'} if item.action == tgedit.PIN_NEW else set()
            )
            if needs - rights:
                run.outcomes += _same((item,), tgedit.NO_RIGHTS)
            elif item.action == tgedit.DESC:
                run.outcomes.append(await self._about(run, item))
            else:
                run.outcomes.append(await self._pin(run, item))

    async def _about(self, run: _Run, item: tgedit.Item) -> tgedit.Outcome:
        """Append to the channel's description."""
        chat = self.deps.channel()
        about = await self.deps.account.about(chat)
        if about is None:
            return tgedit.Outcome(item, tgedit.ERROR, tgedit.FAILED)
        after, why = tgedit.plan_about(about, item, self.guard)
        if why:
            return tgedit.Outcome(item, tgedit.SKIPPED, why)
        if not run.live:
            return tgedit.Outcome(item, tgedit.DRY_OK)
        await self._pace(run)
        self._journal(run.package, 'about', {'about': about})
        landed = await self.deps.account.set_about(chat, after)
        return _one(item, landed=landed)

    async def _pin(self, run: _Run, item: tgedit.Item) -> tgedit.Outcome:
        """Post a new message to the channel and pin it."""
        chat = self.deps.channel()
        recent = await self.deps.account.linked(chat, RECENT)
        guard = self.guard
        why = tgedit.plan_pin([p.text for p in recent], item, guard)
        if why:
            return tgedit.Outcome(item, tgedit.SKIPPED, why)
        if not run.live:
            return tgedit.Outcome(item, tgedit.DRY_OK)
        new = tgedit.read_markup(item.new, guard.emoji)
        await self._pace(run)
        sent = await self.deps.account.send(
            chat, userchat.Text(new.text, spans=new.spans)
        )
        if not sent:
            return tgedit.Outcome(item, tgedit.ERROR, tgedit.FAILED)
        self._journal(run.package, 'sent', {'id': sent})
        await self._pace(run)
        return _one(item, landed=await self.deps.account.pin(chat, sent))

    async def _pace(self, run: _Run) -> None:
        """Keep writes at least ``pause_sec`` apart (none before the first)."""
        if run.writes:
            await self.deps.sleep(run.pause_sec)
        run.writes += 1

    def _journal(
        self, package: str, what: str, body: dict[str, object]
    ) -> None:
        """Append one row: a before-image (before the write) or an outcome."""
        row = {'at': time.time(), 'package': package, 'what': what, **body}
        line = json.dumps(row, ensure_ascii=False) + '\n'
        with self.deps.journal.open('a', encoding='utf-8') as out:
            out.write(line)

    def _report(self, run: _Run, refused: str = '') -> None:
        """Log and journal every item's outcome; remember the package."""
        labels = self.deps.params.labels
        mode = 'on' if run.live else 'dry'
        head = labels.get(f'head_{mode}', f'tg-edit {mode}')
        if refused:
            log.warning(
                '%s %s: %s: %s',
                head,
                run.package,
                labels.get('refused', 'refused'),
                refused,
            )
            self._journal(
                run.package, 'refused', {'mode': mode, 'why': refused}
            )
        else:
            counts = tgedit.tally(run.outcomes)
            summary = ', '.join(
                f'{labels.get(k, k)} {v}' for k, v in sorted(counts.items())
            )
            log.info('%s %s: %s', head, run.package, summary)
            for line in tgedit.report(run.outcomes, labels):
                log.info('tgedit %s: %s', run.package, line)
            for one in run.outcomes:
                self._journal(run.package, 'outcome', _row(one, mode))
        if run.live:
            self.state.done.add(run.package)
        else:
            self.state.dry.add(run.package)
        self.save()
        self.write_report(time.time())


def _row(one: tgedit.Outcome, mode: str) -> dict[str, object]:
    """Return one outcome as a journal row's body."""
    return {
        'mode': mode,
        'post': one.item.post,
        'action': one.item.action,
        'status': one.status,
        'reason': one.reason,
        'count': one.count,
    }


def _same(items: tuple[tgedit.Item, ...], reason: str) -> list[tgedit.Outcome]:
    """Skip every item for one reason."""
    return [tgedit.Outcome(one, tgedit.SKIPPED, reason) for one in items]


def _one(item: tgedit.Item, *, landed: bool, count: int = 1) -> tgedit.Outcome:
    """Return the outcome of one write that moved ``count`` places."""
    if landed:
        return tgedit.Outcome(item, tgedit.APPLIED, count=count)
    return tgedit.Outcome(item, tgedit.ERROR, tgedit.FAILED)


def _landed(
    outcomes: tuple[tgedit.Outcome, ...], *, landed: bool
) -> list[tgedit.Outcome]:
    """Turn a post's dry-run passes into applied (or failed) outcomes."""
    return [
        _one(one.item, landed=landed, count=one.count)
        if one.status == tgedit.DRY_OK
        else one
        for one in outcomes
    ]
