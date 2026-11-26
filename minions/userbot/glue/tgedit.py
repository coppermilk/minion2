# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Apply approved channel-edit packages from the team board.

The Telethon side of ``core/tgedit``. Each tick, inside the persona's
waking hours, it reads the board's notes tagged ``tg-edit``, keeps the
ones a permitted author wrote and an approver accepted, and for each
package not handled yet:

1. checks the account's rights in the channel and the package's limits;
2. reads every post the package names and DRY-RUNS each item against the
   post as it is now (``old`` must match it character for character);
3. ``dry`` mode stops there and posts the dry-run report into the
   package's thread; ``on`` mode journals every post it is about to
   change (text and entities, for an undo by hand), applies what passed,
   one write at least ``pause_sec`` (never under 10 s) after another,
   and posts ``post -> applied / skipped / error`` into the thread.

``mode`` lives in ``engines.tgedit`` and ships ``off``: switching it is a
reviewed change of the constants file, not a chat command. A handled
package is remembered in ``tgedit.json``, so it is applied once; a post
the package does not name is never read, let alone written.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from minion_core.adapters import board as boardapi
from minion_core.adapters import userchat
from minion_core.kernel import atomic_write
from minions.userbot.core import codec
from minions.userbot.core import relink
from minions.userbot.core import tgedit
from minions.userbot.glue.links import chunks

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from collections.abc import Mapping
    from pathlib import Path

log = logging.getLogger('userbot')

STATE_FILE = 'tgedit.json'
JOURNAL_FILE = 'tgedit_journal.jsonl'
MODES = ('off', 'dry', 'on')
REPORT_CHUNK = 3800
"""Characters per board reply (the board's limit is 4000)."""
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
    board_url: str = ''
    tag: str = 'tg-edit'
    authors: tuple[str, ...] = ()
    approval: tgedit.Approval = field(
        default_factory=lambda: tgedit.Approval('', ())
    )
    max_deletes: int = tgedit.MAX_DELETES
    pause_sec: float = tgedit.MIN_PAUSE_SEC
    tick_sec: float = 900.0
    guard: tgedit.Guard = field(default_factory=tgedit.Guard)
    labels: dict[str, str] = field(default_factory=dict)
    clock: relink.Clock = field(default_factory=relink.Clock)


def _words(value: object) -> tuple[str, ...]:
    """Read a JSON list of strings, dropping anything else."""
    return tuple(str(v) for v in codec.rows(value) if isinstance(v, str))


def load_edit_params(settings: Mapping[str, object]) -> EditParams:
    """Read ``engines.tgedit``; an unknown mode reads as ``off``.

    The two hard limits are clamped here, so no constants file can raise
    the deletions cap above 15 or bring two writes closer than 10 s.
    """
    cfg = codec.engine(settings, 'tgedit')
    persona = codec.section(settings, 'persona')
    mode = codec.text(cfg.get('mode'), 'off')
    labels = codec.table(cfg.get('labels'))
    return EditParams(
        mode=mode if mode in MODES else 'off',
        board_url=codec.text(cfg.get('board_url'), '').strip(),
        tag=codec.text(cfg.get('tag'), 'tg-edit'),
        authors=_words(cfg.get('authors')),
        approval=tgedit.Approval(
            word=codec.text(cfg.get('approve_word'), '').strip(),
            approvers=_words(cfg.get('approvers')),
        ),
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
        ),
        labels={str(k): str(v) for k, v in labels.items()},
        clock=relink.Clock(
            tz_offset_hours=codec.num(persona.get('tz_offset_hours'), 0.0),
            wake_start_hour=int(codec.num(persona.get('wake_start_hour'), 0)),
            wake_end_hour=int(codec.num(persona.get('wake_end_hour'), 24)),
        ),
    )


TOKEN_ENV = 'TGEDIT_BOARD_TOKEN'  # noqa: S105 -- the env var's name, not a secret
"""The env var holding the board role's MCP token (never in the JSON)."""


def board_from_env(
    params: EditParams, env: Mapping[str, str]
) -> boardapi.Board | None:
    """Return the board door, or None when the url or the token is unset."""
    token = env.get(TOKEN_ENV, '').strip()
    if not params.board_url or not token:
        return None
    return boardapi.Board(params.board_url, token)


@dataclass(frozen=True)
class EditDeps:
    """Everything the worker may reach; nothing else is in scope."""

    account: userchat.Account
    board: boardapi.Board | None  # None: no token configured, idle
    channel: Callable[[], int]  # the profile's channel (test or live)
    path: Path  # tgedit.json
    journal: Path  # tgedit_journal.jsonl
    params: EditParams
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep


@dataclass
class EditState:
    """What survives a restart: packages applied, packages dry-reported."""

    done: set[int] = field(default_factory=set)
    dry: set[int] = field(default_factory=set)


def read_state(path: Path) -> EditState:
    """Load ``tgedit.json``; an unreadable file starts empty."""
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return EditState()
    if not isinstance(raw, dict):
        return EditState()

    def ids(key: str) -> set[int]:
        return {int(v) for v in codec.rows(raw.get(key)) if isinstance(v, int)}

    return EditState(done=ids('done'), dry=ids('dry'))


def write_state(path: Path, state: EditState) -> None:
    """Persist the state atomically."""
    body = {'done': sorted(state.done), 'dry': sorted(state.dry)}
    atomic_write(path, json.dumps(body, indent=2).encode('utf-8'))


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


def _note(row: boardapi.BoardNote) -> tgedit.Note:
    """Convert a board row into the approval check's note."""
    return tgedit.Note(row.id, row.role, row.text, row.created_at)


class ChannelUnreadableError(RuntimeError):
    """Telegram could not be read; the package waits for the next tick."""


@dataclass
class _Run:
    """One package's pass: outcomes so far and whether writes go out."""

    package: int
    live: bool
    outcomes: list[tgedit.Outcome] = field(default_factory=list)
    writes: int = 0


@dataclass
class ChannelEditor:
    """Apply approved ``tg-edit`` packages to the channel."""

    deps: EditDeps
    state: EditState = field(default_factory=EditState)

    def load(self) -> None:
        """Read the persisted state."""
        self.state = read_state(self.deps.path)

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
        params, board = self.deps.params, self.deps.board
        if params.mode == 'off' or board is None:
            return 0
        if not params.clock.awake(now):
            return 0
        handled = 0
        for package in await self._pending(board):
            await self._handle(board, package)
            handled += 1
        return handled

    async def _pending(
        self, board: boardapi.Board
    ) -> list[boardapi.BoardNote]:
        """Return approved packages not handled in the current mode yet."""
        params = self.deps.params
        seen = self.state.dry if params.mode == 'dry' else self.state.done
        notes = await asyncio.to_thread(
            board.notes, tag=params.tag, kind='note', limit=50
        )
        fresh = [
            note
            for note in sorted(notes, key=lambda n: n.id)
            if note.id not in seen | self.state.done
            and note.role in params.authors
        ]
        return [note for note in fresh if await self._approved(board, note)]

    async def _approved(
        self, board: boardapi.Board, package: boardapi.BoardNote
    ) -> bool:
        """Whether an approver accepted ``package`` anywhere on the board."""
        mentions = await asyncio.to_thread(
            board.notes, q=f'#{package.id}', limit=50
        )
        approval = self.deps.params.approval
        return bool(approval.by(_note(package), [_note(m) for m in mentions]))

    async def _handle(
        self, board: boardapi.Board, package: boardapi.BoardNote
    ) -> None:
        """Dry-run (and in ``on`` mode apply) one package; report it."""
        live = self.deps.params.mode == 'on'
        run = _Run(package.id, live)
        try:
            items = tgedit.parse(package.text)
            tgedit.check_limits(items, self.deps.params.max_deletes)
        except tgedit.PackageError as exc:
            await self._post(board, run, refused=str(exc))
            return
        rights = await self.deps.account.rights(self.deps.channel())
        if rights is None:
            msg = f'tgedit: rights in the channel unreadable (#{package.id})'
            raise ChannelUnreadableError(msg)
        await self._posts(run, items, rights)
        await self._channel(run, items, rights)
        await self._post(board, run)

    async def _posts(
        self, run: _Run, items: tuple[tgedit.Item, ...], rights: frozenset[str]
    ) -> None:
        """Dry-run and apply the items on posts, one post at a time."""
        groups = tgedit.by_post(items)
        chat = self.deps.channel()
        read = await self.deps.account.posts(chat, list(groups))
        if read is None:
            msg = f'tgedit: posts unreadable (#{run.package})'
            raise ChannelUnreadableError(msg)
        for post_id, group in groups.items():
            missing = {NEEDS[one.action] for one in group} - rights
            if missing:
                run.outcomes += _same(group, tgedit.NO_RIGHTS)
                continue
            found = read.get(post_id)
            plan = tgedit.plan_post(
                to_post(found) if found else None,
                group,
                self.deps.params.guard,
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
        edit = userchat.Relinked(plan.text, plan.spans, plan.urls)
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
        after, why = tgedit.plan_about(about, item, self.deps.params.guard)
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
        why = tgedit.plan_pin(
            [p.text for p in recent], item, self.deps.params.guard
        )
        if why:
            return tgedit.Outcome(item, tgedit.SKIPPED, why)
        if not run.live:
            return tgedit.Outcome(item, tgedit.DRY_OK)
        await self._pace(run)
        sent = await self.deps.account.send(chat, userchat.Text(item.new))
        if not sent:
            return tgedit.Outcome(item, tgedit.ERROR, tgedit.FAILED)
        self._journal(run.package, 'sent', {'id': sent})
        await self._pace(run)
        return _one(item, landed=await self.deps.account.pin(chat, sent))

    async def _pace(self, run: _Run) -> None:
        """Keep writes at least ``pause_sec`` apart (none before the first)."""
        if run.writes:
            await self.deps.sleep(self.deps.params.pause_sec)
        run.writes += 1

    def _journal(
        self, package: int, what: str, body: dict[str, object]
    ) -> None:
        """Append one before-image to the journal, before the write."""
        row = {'at': time.time(), 'package': package, 'what': what, **body}
        line = json.dumps(row, ensure_ascii=False) + '\n'
        with self.deps.journal.open('a', encoding='utf-8') as out:
            out.write(line)

    async def _post(
        self, board: boardapi.Board, run: _Run, refused: str = ''
    ) -> None:
        """Report into the package's thread; remember the package."""
        labels = self.deps.params.labels
        mode = 'on' if run.live else 'dry'
        head = labels.get(f'head_{mode}', f'tg-edit {mode}')
        if refused:
            lines = [
                f'{head} #{run.package}',
                labels.get('refused', 'refused') + f': {refused}',
            ]
        else:
            counts = tgedit.tally(run.outcomes)
            summary = ', '.join(
                f'{labels.get(k, k)} {v}' for k, v in sorted(counts.items())
            )
            lines = [
                f'{head} #{run.package}: {summary}',
                *tgedit.report(run.outcomes, labels),
            ]
        for n, text in enumerate(chunks(lines, REPORT_CHUNK), start=1):
            ref = f'tgedit-{run.package}-{mode}-{n}'
            await asyncio.to_thread(board.reply, run.package, text, ref)
        if run.live:
            self.state.done.add(run.package)
        else:
            self.state.dry.add(run.package)
        self.save()
        log.info(
            'tgedit: package %d %s, %d item(s)',
            run.package,
            mode,
            len(run.outcomes),
        )


def _same(items: tuple[tgedit.Item, ...], reason: str) -> list[tgedit.Outcome]:
    """Skip every item for one reason."""
    return [tgedit.Outcome(one, tgedit.SKIPPED, reason) for one in items]


def _one(item: tgedit.Item, *, landed: bool) -> tgedit.Outcome:
    """Return the outcome of one write."""
    if landed:
        return tgedit.Outcome(item, tgedit.APPLIED)
    return tgedit.Outcome(item, tgedit.ERROR, tgedit.FAILED)


def _landed(
    outcomes: tuple[tgedit.Outcome, ...], *, landed: bool
) -> list[tgedit.Outcome]:
    """Turn a post's dry-run passes into applied (or failed) outcomes."""
    return [
        _one(one.item, landed=landed) if one.status == tgedit.DRY_OK else one
        for one in outcomes
    ]
