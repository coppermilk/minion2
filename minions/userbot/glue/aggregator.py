# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Group a Short's per-platform links and post the collected message.

A collaborator, not a mixin. It owns the in-flight groups, the posted log
and the reject set -- the state /status reads off it -- and reaches
everything else through ``AggregatorDeps``. It has exactly one edge to the
reaction side: ``on_posted``, called once per delivered post.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from minion_core.adapters import userchat
from minions.userbot.core.matching import action_ok
from minions.userbot.core.matching import duration_seconds
from minions.userbot.core.matching import extract_fields
from minions.userbot.core.matching import is_recent_repost
from minions.userbot.core.matching import norm
from minions.userbot.core.matching import parse_item
from minions.userbot.core.matching import similar
from minions.userbot.core.models import Group
from minions.userbot.core.models import Item
from minions.userbot.core.models import Posted
from minions.userbot.core.models import iso
from minions.userbot.core.render import compose
from minions.userbot.core.render import youtube_thumb
from minions.userbot.core.runtime import cancel
from minions.userbot.core.statefile import pending_dict
from minions.userbot.core.statefile import pending_from_dict
from minions.userbot.core.statefile import posted_dict
from minions.userbot.core.statefile import posted_from_dict
from minions.userbot.core.statefile import write_state

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from pathlib import Path

    from telethon import TelegramClient

    from minions.userbot.core.humanize import Variety
    from minions.userbot.core.models import Config
    from minions.userbot.core.models import Consts
    from minions.userbot.engines.premium_emoji import PremiumMessage

log = logging.getLogger('userbot')

# Posted videos kept in the readable log; doubles as the restart-dedup window.
POSTED_CAP = 300


@dataclass(frozen=True)
class AggregatorDeps:
    """Everything the poster may reach; nothing else is in scope."""

    client: TelegramClient
    account: userchat.Account
    config: Config
    consts: Consts
    state_path: Path
    targets: Callable[[], tuple[int, ...]]  # the profile's post destinations
    on_posted: Callable[[int, int], Awaitable[None]]  # hand a post onward
    field_keys: tuple[str, ...]  # incoming JSON field names to read
    variety: Variety  # non-repeating picker for the post decoration
    mode: str = 'live'  # for the restore log only


@dataclass
class LinkAggregator:
    """Collect a video's platform links, then post them as one message."""

    deps: AggregatorDeps
    groups: list[Group] = field(default_factory=list)
    posted: list[Posted] = field(default_factory=list)
    rejected: set[str] = field(default_factory=set)
    # source ids already posted (the backfill dedup)
    processed_ids: set[int] = field(default_factory=set)

    def cancel(self) -> None:
        """Cancel every in-flight group timeout (before a mode switch)."""
        for group in self.groups:
            cancel(getattr(group, 'task', None))

    async def on_message(self, msg: userchat.Msg) -> None:
        """Route one incoming message into its video group."""
        log.info(
            'received msg %s: %.120s', msg.id, msg.text.replace('\n', ' ')
        )
        if msg.id in self.processed_ids:
            log.info('msg %s: already posted, skipping', msg.id)
            return
        item = self._accept(msg)
        if item is None:
            return
        group = self._group_for(item)
        if group is None:
            return
        group.items[item.key] = item
        group.msg_ids.add(item.msg_id)
        missing = [
            p for p in self.deps.config.platforms if p not in group.items
        ]
        log.info(
            'caught msg %s (%s) for %r -- have %d/%d, waiting for: %s',
            item.msg_id,
            item.platform,
            group.title,
            len(group.items),
            len(self.deps.config.platforms),
            ', '.join(missing) or 'nothing, complete',
        )
        self._save()
        if not missing:
            await self._flush(group)

    def _accept(self, msg: userchat.Msg) -> Item | None:
        """Parse a message into a Short's item, or None to ignore it."""
        msg_id = msg.id
        text = msg.text
        data = extract_fields(text, self.deps.field_keys)
        if not data:
            log.info('msg %s: no recognizable fields, ignoring', msg_id)
            return None
        if not action_ok(data, self.deps.consts):
            log.info(
                'msg %s: action is not %r, skipping',
                msg_id,
                self.deps.consts.action_value,
            )
            return None
        item = parse_item(data, msg_id, self.deps.consts.fields)
        if item is None or norm(item.title) in self.rejected:
            log.info('msg %s: no platform/caption or already rejected', msg_id)
            return None
        return self._short_or_reject(item, msg_id)

    def _short_or_reject(self, item: Item, msg_id: int) -> Item | None:
        """Return the item if it is a Short, else reject the video and log.

        An empty/absent duration means unknown -- treated as a Short (kept).
        """
        seconds = duration_seconds(item.duration)
        if seconds >= self.deps.config.max_duration:
            log.info(
                'msg %s: %s is %ss (>= %ss) -- not a Short, dropping %r',
                msg_id,
                item.platform,
                seconds,
                self.deps.config.max_duration,
                item.title,
            )
            self._reject(item.title)
            return None
        return item

    def _reject(self, title: str) -> None:
        """Remember a non-Short video and drop any group open for it."""
        self.rejected.add(norm(title))
        group = self._match(title)
        if group is not None and group in self.groups:
            self.groups.remove(group)
            if group.task is not None:
                group.task.cancel()
        self._save()

    def _match(self, title: str) -> Group | None:
        """Return a group whose title is >= threshold similar, or None."""
        norm_title = norm(title)
        for group in self.groups:
            if (
                similar(norm_title, norm(group.title))
                >= self.deps.config.threshold
            ):
                return group
        return None

    def _group_for(self, item: Item) -> Group | None:
        """Return the group this item joins, or None to skip it (dup).

        Joins an in-flight group whose title matches; otherwise starts a new
        one -- unless this video was already posted inside the re-post window,
        in which case it is skipped so the same video is not posted twice.
        """
        group = self._match(item.title)
        if group is not None:
            return group
        if self._recently_posted(item.title):
            log.info(
                'msg %s: %r already posted recently, not re-posting',
                item.msg_id,
                item.title,
            )
            return None
        return self._start(item)

    def _recently_posted(self, title: str) -> bool:
        """Whether this video was already posted inside the re-post window.

        The per-message-id guard cannot catch a video the source re-delivers
        under NEW ids (an upstream re-emit, common once the chat's auto-delete
        clears the old messages); this title guard does. It fires on a match
        in EITHER window: within ``repost_guard`` seconds, or among the last
        ``repost_guard_count`` posted videos. Only consulted when no in-flight
        group matches, so platforms of a video still being collected are never
        blocked.
        """
        return is_recent_repost(
            self.posted,
            title,
            time.time(),
            threshold=self.deps.config.threshold,
            window=self.deps.config.repost_guard,
            count=self.deps.config.repost_guard_count,
        )

    def _start(self, item: Item) -> Group:
        """Create a group for a new video and arm its flush timeout."""
        group = Group(title=item.title)
        self.groups.append(group)
        self._arm(group)
        return group

    def _arm(self, group: Group) -> None:
        """Schedule the group's timeout flush."""
        group.task = asyncio.create_task(self._expire(group))

    async def _expire(self, group: Group) -> None:
        """Flush a group once its timeout (from creation) elapses."""
        remaining = self.deps.config.timeout - (time.time() - group.created_at)
        if remaining > 0:
            await asyncio.sleep(remaining)
        log.info('timeout for %r -- posting what arrived', group.title)
        await self._flush(group)

    async def _flush(self, group: Group) -> None:
        """Post the collected links once, mark the sources, then forget it.

        Ordering is deliberate and durable: we RECORD the post and SAVE state
        the instant the message is delivered, BEFORE the ancillary react/watch
        steps. Those steps do slow, flood-prone Telegram calls; if one of them
        raises or the process is restarted mid-way (common), the message is
        already out. Recording after them -- the old order -- left the post
        unrecorded and the pending group still on disk, so the next restart
        re-flushed it and re-posted the same video again and again. If nothing
        is delivered (flood wait, network), the group is re-queued for a later
        retry instead of being dropped.
        """
        if group not in self.groups:
            return
        self.groups.remove(group)
        if group.task is not None:
            group.task.cancel()
        log.info(
            'posting %r with %d platform(s): %s',
            group.title,
            len(group.items),
            ', '.join(sorted(group.items)),
        )
        message = compose(
            group,
            self.deps.config.platforms,
            self.deps.consts,
            self.deps.variety,
        )
        posts = await self._deliver_post(message, youtube_thumb(group))
        if not posts:
            log.warning('post for %r did not go out; re-queueing', group.title)
            group.created_at = time.time()  # a fresh timeout, not a tight loop
            self.groups.append(group)
            self._arm(group)
            self._save()
            return
        self._record_posted(group)  # commit BEFORE react/watch (see docstring)
        log.info('posted %r', group.title)
        self._save()
        for target, post_id in posts:
            await self.deps.on_posted(target, post_id)

    def _record_posted(self, group: Group) -> None:
        """Append a readable posted record; rebuild the dedup id set."""
        links = {
            key: item.url for key, item in group.items.items() if item.url
        }
        self.posted.append(
            Posted(
                title=group.title,
                at=iso(time.time()),
                links=links,
                msg_ids=sorted(group.msg_ids),
            )
        )
        del self.posted[:-POSTED_CAP]  # keep only the most recent POSTED_CAP
        self.processed_ids = {i for p in self.posted for i in p.msg_ids}

    async def backfill(self) -> None:
        """Scan recent source history for messages not yet processed."""
        limit = self.deps.config.backfill
        if limit <= 0:
            return
        log.info(
            'backfill: scanning last %d messages of %s ...',
            limit,
            self.deps.config.source,
        )
        history = await self.deps.account.history(
            self.deps.config.source, userchat.Slice(limit=limit)
        )
        for message in reversed(history):  # oldest first
            await self.on_message(message)
        log.info('backfill: done (%d messages scanned)', len(history))

    async def _deliver_post(
        self, message: PremiumMessage, thumb: str
    ) -> list[tuple[int, int]]:
        """Send the post to every target; return (target, post_id) delivered.

        Only the SEND is here -- the caller records state from the returned
        list, then does the react/watch steps. A send that raises (flood wait,
        network) is logged and skipped so one bad target neither aborts the
        others nor blocks recording the ones that did go out. An empty result
        means nothing was delivered and the caller should re-queue.
        """
        posts: list[tuple[int, int]] = []
        for target in self.deps.targets():
            try:
                sent = await self._send_post(target, message, thumb)
            except Exception:
                log.exception('send to %s failed', target)
                continue
            posts.append((target, int(getattr(sent, 'id', 0) or 0)))
        return posts

    async def _send_post(
        self, target: int, message: PremiumMessage, thumb: str
    ) -> object:
        """Send one post as a photo (thumb) or text; return the message."""
        if thumb:
            try:
                return await self.deps.client.send_file(
                    target,
                    thumb,
                    caption=message.text,
                    formatting_entities=userchat.entities(
                        message.text, message.spans
                    ),
                )
            except Exception:  # noqa: BLE001 -- bad thumb falls back to text
                log.warning('thumbnail send failed; posting as text')
        return await self.deps.client.send_message(
            target,
            message.text,
            formatting_entities=userchat.entities(message.text, message.spans),
            link_preview=False,
        )

    def _save(self) -> None:
        """Persist state to disk as readable, indented JSON (atomic)."""
        data = {
            'posted': [posted_dict(p) for p in self.posted],
            'pending': [
                pending_dict(g, self.deps.config.platforms)
                for g in self.groups
            ],
            'rejected': sorted(self.rejected),
        }
        write_state(self.deps.state_path, data)

    def restore(self) -> None:
        """Reload saved state and re-arm timers (call once at startup).

        Reads strictly, NOT via ``read_state``: an unreadable file coming
        back empty would read as "nothing was ever posted", disarm the
        re-post guard and re-post the backlog. A parse error propagates.
        """
        if not self.deps.state_path.exists():
            return
        data = json.loads(self.deps.state_path.read_text(encoding='utf-8'))
        self.rejected = set(data.get('rejected') or [])
        self._restore_posted(data)
        self._restore_pending(data)
        log.info(
            'restored %d pending, %d posted (%d dedup ids); mode=%s',
            len(self.groups),
            len(self.posted),
            len(self.processed_ids),
            self.deps.mode,
        )

    def _restore_posted(self, data: dict[str, object]) -> None:
        """Load the posted log; migrate an old processed_ids-only file."""
        self.posted = [posted_from_dict(p) for p in data.get('posted') or []]
        self.processed_ids = {i for p in self.posted for i in p.msg_ids}
        # Back-compat: an old file has no posted log, only raw processed_ids --
        # seed the dedup set from it so a restart still never re-posts.
        self.processed_ids |= set(data.get('processed_ids') or [])

    def _restore_pending(self, data: dict[str, object]) -> None:
        """Load pending groups (new 'pending' key or old 'groups') + re-arm."""
        raw_groups = data.get('pending') or data.get('groups') or []
        for raw in raw_groups:
            group = pending_from_dict(raw)
            self.groups.append(group)
            self._arm(group)
