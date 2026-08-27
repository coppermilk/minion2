# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Link aggregation + posting, mixed into Userbot."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

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
from minions.userbot.core.statefile import pending_dict
from minions.userbot.core.statefile import pending_from_dict
from minions.userbot.core.statefile import posted_dict
from minions.userbot.core.statefile import posted_from_dict
from minions.userbot.core.statefile import write_state

if TYPE_CHECKING:
    from minions.userbot.engines.premium_emoji import PremiumMessage

from minions.userbot.core.base import UserbotProtocol

log = logging.getLogger('userbot')

# Posted videos kept in the readable log; doubles as the restart-dedup window.
POSTED_CAP = 300


class _AggregatorMixin(UserbotProtocol):
    """Link aggregation + posting, mixed into Userbot."""

    async def on_message(self, message: object) -> None:
        """Route one incoming message into its video group."""
        msg_id = int(getattr(message, 'id', 0) or 0)
        preview = (getattr(message, 'message', '') or '').replace('\n', ' ')
        log.info('received msg %s: %.120s', msg_id, preview)
        if msg_id in self.processed_ids:
            log.info('msg %s: already posted, skipping', msg_id)
            return
        item = self._accept(message)
        if item is None:
            return
        group = self._group_for(item)
        if group is None:
            return
        group.items[item.key] = item
        group.msg_ids.add(item.msg_id)
        missing = [p for p in self.config.platforms if p not in group.items]
        log.info(
            'caught msg %s (%s) for %r -- have %d/%d, waiting for: %s',
            item.msg_id,
            item.platform,
            group.title,
            len(group.items),
            len(self.config.platforms),
            ', '.join(missing) or 'nothing, complete',
        )
        self._save()
        if not missing:
            await self._flush(group)

    def _accept(self, message: object) -> Item | None:
        """Parse a message into a Short's item, or None to ignore it."""
        msg_id = int(getattr(message, 'id', 0) or 0)
        text = getattr(message, 'message', '') or ''
        data = extract_fields(text, self._keys)
        if not data:
            log.info('msg %s: no recognizable fields, ignoring', msg_id)
            return None
        if not action_ok(data, self.consts):
            log.info(
                'msg %s: action is not %r, skipping',
                msg_id,
                self.consts.action_value,
            )
            return None
        item = parse_item(data, msg_id, self.consts.fields)
        if item is None or norm(item.title) in self.rejected:
            log.info('msg %s: no platform/caption or already rejected', msg_id)
            return None
        return self._short_or_reject(item, msg_id)

    def _short_or_reject(self, item: Item, msg_id: int) -> Item | None:
        """Return the item if it is a Short, else reject the video and log.

        An empty/absent duration means unknown -- treated as a Short (kept).
        """
        seconds = duration_seconds(item.duration)
        if seconds >= self.config.max_duration:
            log.info(
                'msg %s: %s is %ss (>= %ss) -- not a Short, dropping %r',
                msg_id,
                item.platform,
                seconds,
                self.config.max_duration,
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
            if similar(norm_title, norm(group.title)) >= self.config.threshold:
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
            threshold=self.config.threshold,
            window=self.config.repost_guard,
            count=self.config.repost_guard_count,
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
        remaining = self.config.timeout - (time.time() - group.created_at)
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
            group, self.config.platforms, self.consts, self._variety
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
            await self.comment_watch.on_posted(target, post_id)

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
        limit = self.config.backfill
        if limit <= 0:
            return
        log.info(
            'backfill: scanning last %d messages of %s ...',
            limit,
            self.config.source,
        )
        try:
            history = await self.client.get_messages(
                self.config.source, limit=limit
            )
        except Exception:  # noqa: BLE001 -- source may be unreachable at start
            log.warning('backfill: could not read source history')
            return
        for message in reversed(history):  # oldest first
            await self.on_message(message)
        log.info('backfill: done (%d messages scanned)', len(history))

    def live_targets(self) -> tuple[int, ...]:
        """Post destination for the active profile.

        Test: TEST_CHAT_ID, or the source control chat if it is unset. Live:
        the configured targets. Every channel-touching part reads this, so the
        whole bot follows the profile.
        """
        if self.mode == 'test':
            return (self.config.test_target or self.config.source,)
        return self.config.targets

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
        for target in self.live_targets():
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
                return await self.client.send_file(
                    target,
                    thumb,
                    caption=message.text,
                    formatting_entities=message.entities,
                )
            except Exception:  # noqa: BLE001 -- bad thumb falls back to text
                log.warning('thumbnail send failed; posting as text')
        return await self.client.send_message(
            target,
            message.text,
            formatting_entities=message.entities,
            link_preview=False,
        )

    def _save(self) -> None:
        """Persist state to disk as readable, indented JSON (atomic)."""
        data = {
            'posted': [posted_dict(p) for p in self.posted],
            'pending': [
                pending_dict(g, self.config.platforms) for g in self.groups
            ],
            'rejected': sorted(self.rejected),
        }
        write_state(self.state_path, data)

    def restore(self) -> None:
        """Reload saved state and re-arm timers (call once at startup).

        Reads strictly, NOT via ``read_state``: an unreadable file coming
        back empty would read as "nothing was ever posted", disarm the
        re-post guard and re-post the backlog. A parse error propagates.
        """
        if not self.state_path.exists():
            return
        data = json.loads(self.state_path.read_text(encoding='utf-8'))
        self.rejected = set(data.get('rejected') or [])
        self._restore_posted(data)
        self._restore_pending(data)
        log.info(
            'restored %d pending, %d posted (%d dedup ids); mode=%s',
            len(self.groups),
            len(self.posted),
            len(self.processed_ids),
            self.mode,
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
