# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The cat-reply engine glue, mixed into Aggregator.

Extracted from ``main``: scheduling human-like cat replies to comments,
seeding/rescanning the watch-list, and the send primitives (sticker, text
reply, reaction). ``_CatsMixin`` is mixed into ``Aggregator`` with method
bodies unchanged, so they keep reading ``self`` state; the TYPE_CHECKING
block declares that state for mypy.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from telethon.tl.functions.messages import SendMessageRequest
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import InputReplyToMessage
from telethon.tl.types import ReactionCustomEmoji
from telethon.tl.types import ReactionEmoji

from minions.aggregator import cats
from minions.aggregator.matching import _needs_human
from minions.aggregator.matching import _thread_top
from minions.aggregator.models import _Comment
from minions.aggregator.premium_emoji import RichText
from minions.aggregator.status import STATUS_PENDING_CATS
from minions.aggregator.status import _trim

if TYPE_CHECKING:
    from telethon import TelegramClient
    from telethon import events

    from minions.aggregator.models import Consts
    from minions.aggregator.premium_emoji import PremiumMessage

log = logging.getLogger('aggregator')

# How many recent messages to scan when checking whether the operator already
# replied to a comment by hand (so the bot does not pile a cat on top).
CAT_REPLY_SCAN = 200
# How many existing comments per watched thread to consider at startup, so
# comments made before the bot started can still get a (delayed) cat.
COMMENT_SCAN = 50
# Just before a cat fires we re-scan its post's thread so a fresh comment need
# not wait for the next rescan loop. Throttled per thread to this many seconds
# so a burst of firings costs at most one extra read per thread (flood-safe).
PRE_FIRE_REFRESH_SEC = 45.0


class _CatsMixin:
    """The cat-reply engine, mixed into Aggregator (reads its state)."""

    if TYPE_CHECKING:  # attributes/methods provided by Aggregator (or peers)
        client: TelegramClient
        consts: Consts
        cats: cats.CatBrain
        _cat_tasks: set[asyncio.Task[None]]
        _cat_next_rescan: float
        _rescan_sec: float
        _thread_rescan_at: dict[int, float]

        def live_targets(self) -> tuple[int, ...]: ...

        async def _watch_post(self, target: int, post_id: int) -> None: ...

        async def _send_status(self, text: str) -> None: ...

        def _pending_cat_line(
            self, entry: dict[str, object], now: float
        ) -> str: ...

    def _maybe_cat(self, event: events.NewMessage.Event) -> None:
        """If this message comments on one of our posts, schedule a cat react.

        A "comment" is a reply whose target is one of the last posts. Each
        commenter is catted at most once PER POST -- a second comment under the
        same post is ignored, but the same person on a different post is
        eligible again. The engine decides whether and when (it may return
        nothing -- skipped, silent day, already catted here).
        """
        if getattr(event.message, 'out', False):
            return  # our own message (a post) -- never cat it
        reply = getattr(event.message, 'reply_to', None)
        top = _thread_top(reply)
        chat = int(event.chat_id or 0)
        if not self.cats.is_comment(chat, top):
            return
        person = str(getattr(event, 'sender_id', None) or '')
        if not person:
            return
        # Feedback (principle 8): a reply to our freshest post reads as active
        # engagement, so the reaction comes faster.
        engaged = bool(self.cats.posts) and self.cats.posts[-1] == (chat, top)
        text = _trim(str(getattr(event.message, 'message', '') or ''))
        ref = _Comment(
            chat=chat, root=top, msg_id=int(event.message.id), text=text
        )
        self._schedule_comment(ref, person, engaged=engaged)

    def _schedule_comment(
        self, comment: _Comment, person: str, *, engaged: bool
    ) -> None:
        """Schedule (and arm) a cat for one commenter under a watched post.

        Once per (post, commenter): the dedup key ties the person to THIS
        post's thread, so re-commenting under the same post gets no second cat,
        but the same person on another post is eligible again. The engine may
        return nothing (skipped, silent day, already catted here).

        The cat(s) are CHOSEN here, at schedule time, and stored on the pending
        entry -- so /status and /requeue can show exactly which cat will land
        on which comment, and the send places that same cat rather than a fresh
        random one.
        """
        # When liking everything, key per COMMENT (chat:root:person:msg) so a
        # person's every comment is liked; otherwise once per (post, person).
        # The key keeps the 'chat:root:' prefix so note_post's pruning holds.
        like_all = self.cats.params.like_all
        key = f'{comment.chat}:{comment.root}:{person}'
        if like_all:
            key = f'{key}:{comment.msg_id}'
        when = self.cats.schedule(key, engaged=engaged)
        if when is None:
            return
        # Default is a like REACTION; now and then (deterministic gate) it is a
        # thread STICKER instead -- a premium cat emoji sent as a message. The
        # emoji is pseudo-random but deterministic in the comment id, so the
        # same comment always resolves to the same thing after a restart.
        seed = f'{comment.chat}:{comment.msg_id}'
        post_key = f'{comment.chat}:{comment.root}'
        # A thread STICKER is a message-shaped reply, so it only fits plain
        # enthusiasm; on a question / link / business comment it reads as a
        # non-sequitur. Check content FIRST (so a suppressed sticker does not
        # consume the burst gate), and downgrade to a safe REACTION there.
        allow_sticker = not _needs_human(comment.text, self.consts.human_words)
        sticker = (
            not like_all
            and allow_sticker
            and self.cats.should_sticker(post_key)
        )
        if sticker:
            specs, kind = self.cats.pick_cat(seed), 'reply'
        else:  # like_all always places a like reaction (never a sticker)
            specs, kind = self.cats.pick_like(seed), 'react'
        if not specs:  # empty pool -> nothing to place
            return
        cat = cats.Cat(
            chat=comment.chat,
            reply_to=comment.msg_id,
            root=comment.root,
            when=when,
            text=comment.text,
            emojis=tuple((s.emoji_id, s.fallback) for s in specs),
            kind=kind,
        )
        self.cats.add_pending(cat)
        self._arm_cat(cat)

    def _arm_cat(self, cat: cats.Cat) -> None:
        """Create the fire-later task for a scheduled (persisted) cat."""
        task = asyncio.create_task(self._cat_later(cat))
        self._cat_tasks.add(task)
        task.add_done_callback(self._cat_tasks.discard)

    def rearm_cats(self) -> None:
        """Re-arm cats that were scheduled before a restart (survive downtime).

        Any whose time passed while the host was down is renewed to a fresh
        in-window slot by the engine, so a night's worth does not fire at once.
        """
        for cat in self.cats.rearm():
            self._arm_cat(cat)

    async def backfill_cat_posts(self) -> None:
        """Seed the cat watch-list from the posts already in each target.

        Without this, cats only watch posts made AFTER the bot starts noting
        them, so posts that predate a deploy/restart are ignored. Here we look
        up the last ``watch_posts`` real posts per target and register them (in
        the channel case, resolving each one's discussion thread), so comments
        on the existing last posts get cats right away.
        """
        if not self.cats.params.enabled:
            return
        for target in self.live_targets():  # test mode -> the test channel
            await self._seed_target_posts(target)
        log.info('cats: watch-list has %d post(s)', len(self.cats.posts))

    async def _recent_target_posts(self, target: int, want: int) -> object:
        """Return the last ``want`` posts in a target (channel/group)."""
        if self.cats.params.comments_in_discussion:
            return await self.client.get_messages(target, limit=want)
        return await self.client.get_messages(
            target, limit=want, from_user='me'
        )

    async def _seed_target_posts(self, target: int) -> None:
        """Register the last posts of one target into the cat watch-list."""
        want = self.cats.params.watch_posts
        try:
            history = await self._recent_target_posts(target, want)
        except Exception:  # noqa: BLE001 -- unreachable target: skip, no crash
            log.warning('cats: could not read %s post history', target)
            return
        for message in reversed(list(history)):  # oldest first -> newest last
            msg_id = int(getattr(message, 'id', 0) or 0)
            if msg_id:
                await self._watch_post(target, msg_id)

    async def backfill_cat_comments(self) -> None:
        """Schedule cats for comments already sitting under the watched posts.

        Live events only cover comments that arrive WHILE the bot runs, so
        comments made before it started would never get a cat. Here we scan the
        existing comments in each watched thread and schedule the ones not yet
        catted (dedup, skip and the manual-reply check still apply), spread by
        the engine's heavy-tailed spacing so they trickle out, not burst.
        """
        if not self.cats.params.enabled:
            return
        for chat, root in list(self.cats.posts):
            await self._seed_thread_comments(chat, root)
        log.info('cats: %d comment(s) queued', len(self.cats.state.pending))

    async def cat_rescan_loop(self) -> None:
        """Periodically re-scan targets so new posts are picked up by itself.

        A post created (and commented on) while the bot runs is not
        auto-watched by the event stream; without this the operator had to run
        /requeue by hand. Every ``rescan_sec`` this re-seeds the watch-list
        from each target's recent posts and schedules cats for new comments
        (dedup skips what is already queued/answered). ``_cat_next_rescan`` is
        published for the /status countdown. Off when rescan_sec <= 0.
        """
        period = self._rescan_sec
        if not self.cats.params.enabled or period <= 0:
            return
        while True:
            self._cat_next_rescan = time.time() + period
            await asyncio.sleep(period)
            try:
                await self.backfill_cat_posts()
                await self.backfill_cat_comments()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('cats: periodic rescan failed; will retry')

    async def _seed_thread_comments(self, chat: int, root: int) -> None:
        """Schedule cats for the recent comments in one watched thread."""
        try:
            comments = await self.client.get_messages(
                chat, reply_to=root, limit=COMMENT_SCAN
            )
        except Exception:  # noqa: BLE001 -- no thread/unreachable: skip quietly
            log.warning('cats: could not read comments of %s/%s', chat, root)
            return
        for message in reversed(list(comments)):  # oldest first
            self._schedule_from_message(chat, root, message)

    def _schedule_from_message(
        self, chat: int, root: int, message: object
    ) -> None:
        """Schedule a cat for one existing comment message (skip our own)."""
        if getattr(message, 'out', False):
            return
        person = str(getattr(message, 'sender_id', None) or '')
        comment_id = int(getattr(message, 'id', 0) or 0)
        if person and comment_id:
            text = _trim(str(getattr(message, 'message', '') or ''))
            ref = _Comment(chat=chat, root=root, msg_id=comment_id, text=text)
            self._schedule_comment(ref, person, engaged=False)

    async def requeue_cats(self) -> None:
        """Rescan + refresh the pending-cat queue on demand (the /requeue cmd).

        First re-times and re-arms the PERSISTED queue (so a queue scheduled
        under stale timing is flushed). Then it RESCANS the targets: it
        re-seeds the watch-list from each target's recent posts and schedules
        cats for comments not yet queued -- so a post created (and commented
        on) WHILE the bot was running, which is not auto-watched, is picked up
        here instead of returning "nothing queued". Dedup, skip and the
        manual-reply check still apply, so nothing is duplicated.
        """
        self._cancel_cat_tasks()
        for cat in self.cats.rearm(renew_all=True):  # re-time existing queue
            self._arm_cat(cat)
        await self.backfill_cat_posts()  # pick up posts made since startup
        await self.backfill_cat_comments()  # queue their new comments (armed)
        count = len(self.cats.state.pending)
        await self._send_status(await self._plan_text(f'Requeued {count}'))
        log.info('requeued %d pending cats', count)

    async def answer_all_now(self) -> None:
        """Answer EVERY pending commenter immediately (the /catnow command).

        The human-like wait is bypassed: all pending cats are set to fire now
        (the manual-reply check still applies). An operator override. The reply
        lists exactly which cat lands on which comment, so it is never a
        mystery which reaction /catnow placed.
        """
        self._cancel_cat_tasks()
        due = self.cats.due_now()
        for cat in due:
            self._arm_cat(cat)
        await self._send_status(await self._plan_text(f'Answering {len(due)}'))
        log.info('answering %d pending cats now', len(due))

    async def _plan_text(self, head: str) -> str:
        """`<head> pending cat(s):` then a which-cat-where line for each.

        This is what makes /requeue and /catnow legible: every queued reaction
        is listed with the exact cat, the comment, its post, and the eta -- so
        the operator sees the plan instead of a count.
        """
        pending = self.cats.state.pending
        if not pending:
            return f'{head} pending cat(s). Nothing queued.'
        now = time.time()
        lines = [f'{head} pending cat(s):']
        lines.extend(
            self._pending_cat_line(entry, now)
            for entry in pending[:STATUS_PENDING_CATS]
        )
        extra = len(pending) - STATUS_PENDING_CATS
        if extra > 0:
            lines.append(f'    ... (+{extra} more)')
        return '\n'.join(lines)

    def _cancel_cat_tasks(self) -> None:
        """Cancel every in-flight fire-later cat task."""
        for task in list(self._cat_tasks):
            task.cancel()
        self._cat_tasks.clear()

    async def _refresh_before_fire(self, cat: cats.Cat) -> None:
        """Pull new comments in this post's thread just before we like it.

        A comment made between rescan loops would otherwise wait for the next
        session; re-reading the thread here queues it now. Debounced per thread
        (``PRE_FIRE_REFRESH_SEC``) so a burst of due cats reads it once.
        """
        if not self.cats.params.enabled:
            return
        root = cat.root or cat.reply_to
        now = time.time()
        if now - self._thread_rescan_at.get(root, 0.0) < PRE_FIRE_REFRESH_SEC:
            return
        self._thread_rescan_at[root] = now
        try:
            await self._seed_thread_comments(cat.chat, root)
        except Exception:  # noqa: BLE001 -- best effort; the cat still fires
            log.warning('cat: pre-fire refresh failed for thread %s', root)

    async def _cat_later(self, cat: cats.Cat) -> None:
        """Sleep until the cat is due, then react unless answered by hand.

        A send failure is logged loudly (not swallowed) and the entry is
        dropped so one poison comment cannot wedge the queue; the person stays
        catted, so it is not rescheduled.
        """
        delay = cat.when - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        await self._refresh_before_fire(cat)
        try:
            if not await self._should_skip_cat(cat.chat, cat.reply_to):
                await self._deliver(cat)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                'cat: react failed in %s (comment %s)', cat.chat, cat.reply_to
            )
        self.cats.done_pending(cat.chat, cat.reply_to)

    async def _should_skip_cat(self, chat: int, comment_id: int) -> bool:
        """Skip the cat when the operator already answered the comment by hand.

        "By hand" is either a manual reply to the comment OR a manual reaction
        already sitting on it -- in both cases the operator has engaged, so the
        bot does not pile a cat reaction on top.
        """
        if not self.cats.params.skip_if_manually_replied:
            return False
        answered = await self._human_answered(chat, comment_id)
        if answered:
            log.info('cat: %s already answered by hand, skipping', comment_id)
        return answered

    async def _human_answered(self, chat: int, comment_id: int) -> bool:
        """Whether the operator already answered ``comment_id`` by hand.

        Two hand signals count, either one wins: an outgoing (manual) reply to
        the comment, or this account's own reaction already sitting on it. The
        cat has not been placed yet, so any such reply/reaction is the
        operator's own -- do not pile a cat reaction on top of it.
        """
        try:
            history = await self.client.get_messages(
                chat, limit=CAT_REPLY_SCAN
            )
        except Exception:  # noqa: BLE001 -- unreachable: fail open, react anyway
            log.warning(
                'cat: could not check a manual reply to %s', comment_id
            )
            return False
        for message in history:
            if not getattr(message, 'out', False):
                continue
            reply = getattr(message, 'reply_to', None)
            if getattr(reply, 'reply_to_msg_id', None) == comment_id:
                return True
        return await self._own_reaction(chat, comment_id)

    async def _own_reaction(self, chat: int, comment_id: int) -> bool:
        """Whether this account's own reaction already sits on the comment.

        The reliable signal is the reaction TALLY: Telegram sets
        ``chosen_order`` on every ``results`` entry the current account
        picked, so a non-None chosen_order means "we already reacted here" no
        matter how many others reacted after (unlike ``recent_reactions``,
        which is a short, capacity-capped list). We check the tally first and
        fall back to ``recent_reactions[].my`` for older layers. Best-effort
        and fail-open: an unreadable comment reacts anyway rather than wedging
        the queue.
        """
        try:
            message = await self.client.get_messages(chat, ids=comment_id)
        except Exception:  # noqa: BLE001 -- unreachable: fail open, react anyway
            return False
        reactions = getattr(message, 'reactions', None)
        results = getattr(reactions, 'results', None) or []
        if any(getattr(r, 'chosen_order', None) is not None for r in results):
            return True
        recent = getattr(reactions, 'recent_reactions', None) or []
        return any(getattr(r, 'my', False) for r in recent)

    async def _deliver(self, cat: cats.Cat) -> None:
        """Place the scheduled cat: a like REACTION, or a thread STICKER.

        ``cat.kind`` was decided at schedule time and stored, so the delivery
        is exactly what /status showed: 'react' puts a like reaction ON the
        comment; 'reply' sends the chosen premium cat emoji as a message in the
        comment's thread (it reads like a sticker).
        """
        if cat.kind == 'reply':
            await self._send_sticker(cat)
        else:
            await self._send_cats(cat)

    async def _send_cats(self, cat: cats.Cat) -> None:
        """React to the commenter's message with the cat(s) chosen at schedule.

        The reaction is placed ON the comment itself -- the cat emoji shows as
        a reaction pill under the commenter's message, not as a reply in the
        thread. The emoji were picked when the comment was scheduled and stored
        on ``cat``, so what lands is exactly what /status showed.
        """
        placed = await self._react(cat.chat, cat.reply_to, cat.emojis)
        if placed:
            glyphs = ''.join(fb for _, fb in cat.emojis)
            log.info(
                'cat: reacted %s on comment %s in %s',
                glyphs,
                cat.reply_to,
                cat.chat,
            )

    async def _send_sticker(self, cat: cats.Cat) -> None:
        """Reply IN THE THREAD with the chosen premium cat emoji (a sticker).

        The emoji is sent as a message that replies to the comment inside its
        discussion thread (top=root), so it lands in the post's comments and
        reads like a sticker -- not a reaction pill. Falls back to a flat reply
        if the threaded send is refused (or it is a plain group), so a sticker
        is never lost to threading.
        """
        if not cat.emojis:
            return
        emoji_id, fallback = cat.emojis[0]
        spec = {'id': emoji_id, 'fallback': fallback}
        message = RichText().emoji(spec).build()
        threaded = bool(cat.root) and cat.root != cat.reply_to
        if threaded:
            try:
                await self._reply_in_thread(cat, message)
            except Exception:  # noqa: BLE001 -- fall back to a flat reply
                log.warning(
                    'cat: threaded sticker failed in %s; flat', cat.chat
                )
            else:
                log.info(
                    'cat: sticker %s in thread %s of %s',
                    fallback,
                    cat.root,
                    cat.chat,
                )
                return
        await self.client.send_message(
            cat.chat,
            message.text,
            formatting_entities=message.entities,
            reply_to=cat.reply_to,
            link_preview=False,
        )
        log.info(
            'cat: sticker %s on comment %s in %s',
            fallback,
            cat.reply_to,
            cat.chat,
        )

    async def _reply_in_thread(
        self, cat: cats.Cat, message: PremiumMessage
    ) -> None:
        """Send ``message`` as a reply inside the comment thread (top=root)."""
        reply = InputReplyToMessage(
            reply_to_msg_id=cat.reply_to, top_msg_id=cat.root
        )
        await self.client(
            SendMessageRequest(
                peer=cat.chat,
                message=message.text,
                entities=message.entities,
                reply_to=reply,
                no_webpage=True,
            )
        )

    async def _react(
        self, peer: int, msg_id: int, emojis: tuple[tuple[str, str], ...]
    ) -> bool:
        """Place the given premium cat(s) as a reaction ON ``msg_id``.

        ``emojis`` are the ``(id, fallback)`` cats chosen up front. The whole
        set goes in ONE ``SendReaction`` call (reactions are atomic: one
        request carries the account's whole reaction set on the message).
        Returns whether anything was placed (False for an empty set). Shared by
        the post-react and comment-react paths.
        """
        if not emojis:
            return False
        custom = [
            ReactionCustomEmoji(document_id=int(eid)) for eid, _ in emojis
        ]
        try:
            await self._send_reaction(peer, msg_id, custom)
        except Exception:  # noqa: BLE001 -- custom emoji may be disallowed
            # The chat may not allow CUSTOM-emoji reactions (or the account
            # is not Premium): fall back to the plain-emoji version of the
            # same cats (the fallback glyphs), so a cat reaction still lands
            # wherever standard reactions are allowed. If that fails too, it
            # propagates to the caller's guard (logged, never fatal).
            standard = [ReactionEmoji(emoticon=fb) for _, fb in emojis]
            await self._send_reaction(peer, msg_id, standard)
            log.info(
                'cat: custom reaction rejected in %s; used standard emoji',
                peer,
            )
        return True

    async def _send_reaction(
        self, peer: int, msg_id: int, reaction: list[object]
    ) -> None:
        """One SendReaction call placing ``reaction`` on ``msg_id``."""
        await self.client(
            SendReactionRequest(
                peer=peer,
                msg_id=msg_id,
                reaction=reaction,
                add_to_recent=True,
            )
        )
