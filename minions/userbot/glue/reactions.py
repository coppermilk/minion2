# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The Telethon side of the reaction brain: watch comments, place reactions.

A collaborator, not a mixin. It owns the watch-list of posts, the fire-later
timers, and the rendering of its own queued rows; everything else arrives in
``CommentDeps``. That includes the two edges the poster used to share
through ``self``: it asks for the profile's live targets rather than reading
them, and the poster now calls ``on_posted`` instead of reaching in here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from typing import TYPE_CHECKING

from minion_core.adapters import userchat
from minions.userbot.core import tasks
from minions.userbot.core.matching import needs_human
from minions.userbot.core.models import Comment
from minions.userbot.core.models import Emoji
from minions.userbot.core.render import Glyphs
from minions.userbot.core.render import emoji_markup
from minions.userbot.core.render import trim
from minions.userbot.core.runtime import fmt_eta
from minions.userbot.engines import reactions
from minions.userbot.engines.premium_emoji import RichText

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable


log = logging.getLogger('userbot')

# How many recent messages to scan when checking whether the operator already
# replied to a comment by hand (so the bot does not pile a reaction on top).
MANUAL_REPLY_SCAN = 200
# How many existing comments per watched thread to consider at startup, so
# comments made before the bot started can still get a (delayed) reaction.
COMMENT_SCAN = 50
# Just before a reaction fires we re-scan its post's thread so a fresh comment
# need not wait for the next rescan loop. Throttled per thread to this many
# seconds so a burst of firings costs at most one extra read per thread.
PRE_FIRE_REFRESH_SEC = 45.0
# How many queued rows to list before summing the rest as "... (+N more)".
QUEUED_ROWS = 12


def _queued_markup(queued: reactions.Reaction) -> str:
    """Render a queued reaction's chosen emoji as premium markup, or '?'.

    A reaction scheduled before the emoji were stored shows '?' -- a
    /requeue does not re-pick it (the choice is made at schedule time),
    it only re-times what is already there.
    """
    return ''.join(emoji_markup(e, f) for e, f in queued.emojis) or '?'


@dataclass(frozen=True)
class CommentDeps:
    """Everything the comment watcher may reach; nothing else is in scope."""

    account: userchat.Account
    brain: reactions.ReactionBrain
    targets: Callable[[], tuple[int, ...]]  # the profile's live targets
    announce: Callable[[str], Awaitable[None]]  # operator reply, premium
    glyphs: Glyphs = field(default_factory=Glyphs)
    human_words: tuple[str, ...] = ()  # terms that suppress a sticker
    rescan_sec: float = 300.0  # 0 turns the auto-rescan off


@dataclass
class CommentWatch:
    """React to people who comment on the last posts, on human-like timers."""

    deps: CommentDeps
    next_rescan: float = 0.0  # ts of the next auto-rescan, shown by /status
    _timers: set[asyncio.Task[None]] = field(default_factory=set)
    # pre-fire thread refresh debounce, keyed by thread root
    _thread_seen: dict[int, float] = field(default_factory=dict)

    def queued_rows(self) -> list[str]:
        """One capped row per queued reaction, for /status and /requeue."""
        now = time.time()
        rows = [
            self._queued_row(entry, now)
            for entry in self.deps.brain.state.pending
        ]
        if len(rows) <= QUEUED_ROWS:
            return rows
        extra = len(rows) - QUEUED_ROWS
        return [*rows[:QUEUED_ROWS], f'    ... (+{extra} more)']

    def _queued_row(self, queued: reactions.Reaction, now: float) -> str:
        """One queued line: reaction, verb, comment, post, eta."""
        b = self.deps.glyphs.bullet
        what = (
            f'"{queued.text}"' if queued.text else f'comment {queued.reply_to}'
        )
        verb = 'sticker' if queued.kind == 'reply' else 'like'
        eta = queued.when - now
        when = 'due now' if eta <= 0 else f'in ~{fmt_eta(eta)}'
        return (
            f'    {_queued_markup(queued)} {verb} {self.deps.glyphs.arrow}'
            f' {what} {b} post {queued.root} {b} {when}'
        )

    def on_message(self, msg: userchat.Msg) -> None:
        """If this message comments on one of our posts, schedule a reaction.

        A "comment" is a reply whose target is one of the last posts. Each
        commenter is reacted at most once PER POST -- a second comment under
        the
        same post is ignored, but the same person on a different post is
        eligible again. The engine decides whether and when (it may return
        nothing -- skipped, silent day, already reacted here).
        """
        if msg.out or not msg.sender_id:
            return  # our own message (a post) -- never reaction it
        if not self.deps.brain.is_comment(msg.chat_id, msg.root):
            return
        # Feedback (principle 8): a reply to our freshest post reads as active
        # engagement, so the reaction comes faster.
        posts = self.deps.brain.posts
        engaged = bool(posts) and posts[-1] == (msg.chat_id, msg.root)
        ref = Comment(
            chat=msg.chat_id,
            root=msg.root,
            msg_id=msg.id,
            text=trim(msg.text),
            mine_reacted=msg.mine_reacted,
        )
        self._schedule_comment(ref, msg.sender_id, engaged=engaged)

    def _schedule_comment(
        self, comment: Comment, person: int, *, engaged: bool
    ) -> None:
        """Schedule (and arm) a reaction for a commenter under a post.

        Once per (post, commenter): the dedup key ties the person to THIS
        post's thread, so re-commenting under the same post gets no second
        reaction,
        but the same person on another post is eligible again. The engine may
        return nothing (skipped, silent day, already reacted here).

        The reaction(s) are CHOSEN here, at schedule time, and stored on the
        pending
        entry -- so /status and /requeue can show exactly which reaction will
        land
        on which comment, and the send places that same reaction rather than a
        fresh
        random one.
        """
        when = self.deps.brain.schedule(
            self._dedup_key(comment, person), engaged=engaged
        )
        if when is None:
            return
        if self._took_by_hand(comment, person):
            return
        # Berlyne exposure control: like only a Wundt-peak fraction of a
        # person's comments. A steered skip still leaves the key recorded as
        # decided (schedule marked it), so a rescan never re-rolls it into a
        # like.
        attach = self.deps.brain.params.attach_enabled
        if attach and not self.deps.brain.decide_engage(
            person, comment.msg_id
        ):
            return
        # Choose the like reaction vs. the rarer thread sticker (deterministic
        # in the comment id), then place it.
        chosen = self._choose_reaction(person, comment)
        if chosen is None:  # empty pool -> nothing to place
            return
        specs, kind = chosen
        reaction = reactions.Reaction(
            chat=comment.chat,
            reply_to=comment.msg_id,
            root=comment.root,
            when=when,
            text=comment.text,
            emojis=tuple((s.id, s.fallback) for s in specs),
            kind=kind,
        )
        self.deps.brain.add_pending(reaction)
        self._arm_reaction(reaction)

    def _dedup_key(self, comment: Comment, person: int) -> str:
        """Return the key that decides this comment once, and only once.

        When liking everything OR steering exposure per person, key per
        COMMENT so each comment is decided on its own; otherwise once per
        (post, person). The key keeps the ``chat:root:`` prefix either way so
        ``note_post``'s pruning holds. Both flags widen it the same way;
        where they disagree ``attach`` decides, through ``decide_engage``,
        which runs after the schedule and can still say no.
        """
        params = self.deps.brain.params
        key = f'{comment.chat}:{comment.root}:{person}'
        if params.like_all or params.attach_enabled:
            return f'{key}:{comment.msg_id}'
        return key

    def _took_by_hand(self, comment: Comment, person: int) -> bool:
        """Count the operator's own reaction on this comment, if there is one.

        They got there first, so the chance was taken -- by hand, but taken.
        Counting it and standing down replaces rolling for it, which used to
        write ``ignore`` against a comment the operator had personally
        answered and then teach the curve we were neglecting somebody we had
        just engaged.

        The signal rides along from the scan, so knowing costs no call. The
        same flag that stops the bot piling a reaction on top governs it: with
        that off the operator wants us to react anyway, and counting their
        like here as well would put two takes on one comment.

        Reached at most once per comment -- ``schedule`` returns None for a
        key already decided -- which is what keeps the count from repeating
        on every rescan.
        """
        if not comment.mine_reacted:
            return False
        if not self.deps.brain.params.skip_if_manually_replied:
            return False
        self.deps.brain.take_by_hand(person, comment.msg_id)
        return True

    def _choose_reaction(
        self, person: int, comment: Comment
    ) -> tuple[list[Emoji], str] | None:
        """Pick (emoji specs, kind) for this comment: a like or a sticker.

        A thread STICKER is a message-shaped reply, so it only fits plain
        enthusiasm; on a question/link/business comment it reads as a
        non-sequitur, so those stay a like reaction. The emoji is deterministic
        in the comment id (same reaction after a restart). None when the pool
        is empty (nothing to place).
        """
        seed = f'{comment.chat}:{comment.msg_id}'
        allow_sticker = not needs_human(comment.text, self.deps.human_words)
        # The reciprocity control decides sticker vs like at our target rate
        # (no activity-burst gate); a question/link comment stays a plain like.
        if self.deps.brain.decide_sticker(
            person, comment.msg_id, content_ok=allow_sticker
        ):
            specs, kind = self.deps.brain.pick_reaction(seed), 'reply'
        else:
            specs, kind = self.deps.brain.pick_like(seed), 'react'
        return (specs, kind) if specs else None

    def _arm_reaction(self, reaction: reactions.Reaction) -> None:
        """Create the fire-later task for a scheduled (persisted) reaction."""
        tasks.spawn(self._timers, self._reaction_later(reaction))

    def rearm(self) -> None:
        """Re-arm reactions scheduled before a restart (survive downtime).

        Any whose time passed while the host was down is renewed to a fresh
        in-window slot by the engine, so a night's worth does not fire at once.
        """
        for reaction in self.deps.brain.rearm():
            self._arm_reaction(reaction)

    async def seed_posts(self) -> None:
        """Seed the reaction watch-list from the posts already in each target.

        Without this, reactions only watch posts made AFTER the bot starts
        noting
        them, so posts that predate a deploy/restart are ignored. Here we look
        up the last ``watch_posts`` real posts per target and register them (in
        the channel case, resolving each one's discussion thread), so comments
        on the existing last posts get reactions right away.
        """
        if not self.deps.brain.params.enabled:
            return
        for target in self.deps.targets():  # test mode -> the test channel
            await self._seed_target_posts(target)
        log.info(
            'reactions: watch-list has %d post(s)', len(self.deps.brain.posts)
        )

    async def _seed_target_posts(self, target: int) -> None:
        """Register a target's last posts into the reaction watch-list.

        In a channel with comments the posts are ours by definition; in a
        plain group only our own messages are posts, hence ``mine``.
        """
        params = self.deps.brain.params
        history = await self.deps.account.history(
            target,
            userchat.Slice(
                limit=params.watch_posts,
                mine=not params.comments_in_discussion,
            ),
        )
        for message in reversed(history):  # oldest first -> newest last
            if message.id:
                await self._watch_post(target, message.id)

    async def seed_comments(self) -> None:
        """Schedule reactions for comments already under the watched posts.

        Live events only cover comments that arrive WHILE the bot runs, so
        comments made before it started would never get a reaction. Here we
        scan the
        existing comments in each watched thread and schedule the ones not yet
        reacted (dedup, skip and the manual-reply check still apply), spread by
        the engine's heavy-tailed spacing so they trickle out, not burst.
        """
        if not self.deps.brain.params.enabled:
            return
        for chat, root in list(self.deps.brain.posts):
            await self._seed_thread_comments(chat, root)
        log.info(
            'reactions: %d comment(s) queued',
            len(self.deps.brain.state.pending),
        )

    async def rescan_loop(self) -> None:
        """Periodically re-scan targets so new posts are picked up by itself.

        A post created (and commented on) while the bot runs is not
        auto-watched by the event stream; without this the operator had to run
        /requeue by hand. Every ``rescan_sec`` this re-seeds the watch-list
        from each target's recent posts and schedules reactions for new
        comments
        (dedup skips what is already queued/answered). ``_react_next_rescan``
        is
        published for the /status countdown. Off when rescan_sec <= 0.
        """
        period = self.deps.rescan_sec
        if not self.deps.brain.params.enabled or period <= 0:
            return
        while True:
            self.next_rescan = time.time() + period
            await asyncio.sleep(period)
            try:
                await self.seed_posts()
                await self.seed_comments()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('reactions: periodic rescan failed; will retry')

    async def _seed_thread_comments(self, chat: int, root: int) -> None:
        """Schedule reactions for the recent comments in one watched thread."""
        comments = await self.deps.account.history(
            chat, userchat.Slice(limit=COMMENT_SCAN, under=root)
        )
        for message in reversed(comments):  # oldest first
            self._schedule_from_message(chat, root, message)

    def _schedule_from_message(
        self, chat: int, root: int, message: userchat.Msg
    ) -> None:
        """Schedule a reaction for one existing comment (skip our own)."""
        if message.out or not message.sender_id or not message.id:
            return
        ref = Comment(
            chat=chat,
            root=root,
            msg_id=message.id,
            text=trim(message.text),
            mine_reacted=message.mine_reacted,
        )
        self._schedule_comment(ref, message.sender_id, engaged=False)

    async def requeue(self) -> None:
        """Rescan + refresh the pending-reaction queue (the /requeue cmd).

        First re-times and re-arms the PERSISTED queue (so a queue scheduled
        under stale timing is flushed). Then it RESCANS the targets: it
        re-seeds the watch-list from each target's recent posts and schedules
        reactions for comments not yet queued -- so a post created (and
        commented
        on) WHILE the bot was running, which is not auto-watched, is picked up
        here instead of returning "nothing queued". Dedup, skip and the
        manual-reply check still apply, so nothing is duplicated.
        """
        self.cancel()
        for reaction in self.deps.brain.rearm(
            renew_all=True
        ):  # re-time existing queue
            self._arm_reaction(reaction)
        await self.seed_posts()  # pick up posts made since startup
        await self.seed_comments()  # queue their new comments (armed)
        count = len(self.deps.brain.state.pending)
        await self.deps.announce(self._plan_text(f'Requeued {count}'))
        log.info('requeued %d pending reactions', count)

    async def answer_now(self) -> None:
        """Answer EVERY pending commenter immediately (the /reactnow command).

        The human-like wait is bypassed: all pending reactions are set to fire
        now
        (the manual-reply check still applies). An operator override. The reply
        lists exactly which reaction lands on which comment, so it is never a
        mystery which reaction /reactnow placed.
        """
        self.cancel()
        due = self.deps.brain.due_now()
        for reaction in due:
            self._arm_reaction(reaction)
        await self.deps.announce(self._plan_text(f'Answering {len(due)}'))
        log.info('answering %d pending reactions now', len(due))

    def _plan_text(self, head: str) -> str:
        """`<head> pending reaction(s):` then a which-where line each.

        This is what makes /requeue and /reactnow legible: the operator sees
        the exact reaction, comment, post and eta of every queued item, not a
        count. The same rows /status shows -- ``queued_react_rows`` builds
        both.
        """
        rows = self.queued_rows()
        if not rows:
            return f'{head} pending reaction(s). Nothing queued.'
        return '\n'.join([f'{head} pending reaction(s):', *rows])

    def cancel(self) -> None:
        """Cancel every in-flight fire-later reaction task."""
        tasks.cancel_all(self._timers)

    async def _refresh_before_fire(self, reaction: reactions.Reaction) -> None:
        """Pull new comments in this post's thread just before we like it.

        A comment made between rescan loops would otherwise wait for the next
        session; re-reading the thread here queues it now. Debounced per thread
        (``PRE_FIRE_REFRESH_SEC``) so a burst of due reactions reads it once.
        """
        if not self.deps.brain.params.enabled:
            return
        root = reaction.root or reaction.reply_to
        now = time.time()
        if now - self._thread_seen.get(root, 0.0) < PRE_FIRE_REFRESH_SEC:
            return
        self._thread_seen[root] = now
        try:
            await self._seed_thread_comments(reaction.chat, root)
        except Exception:  # noqa: BLE001 -- best effort; the reaction still fires
            log.warning(
                'reaction: pre-fire refresh failed for thread %s', root
            )

    async def _reaction_later(self, reaction: reactions.Reaction) -> None:
        """Sleep until the reaction is due, then react unless answered by hand.

        A send failure is logged loudly (not swallowed) and the entry is
        dropped so one poison comment cannot wedge the queue; the person stays
        reacted, so it is not rescheduled.
        """
        delay = reaction.when - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        await self._refresh_before_fire(reaction)
        try:
            if not await self._should_skip_reaction(reaction):
                await self._deliver(reaction)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                'reaction: react failed in %s (comment %s)',
                reaction.chat,
                reaction.reply_to,
            )
        self.deps.brain.done_pending(reaction.chat, reaction.reply_to)

    async def _should_skip_reaction(
        self, reaction: reactions.Reaction
    ) -> bool:
        """Skip the reaction if the operator already answered by hand.

        "By hand" is either a manual reply to the comment OR a manual reaction
        already sitting on it -- in both cases the operator has engaged, so the
        bot does not pile a reaction on top.

        A written reply is also RECORDED, because it is the strong act: it is
        what the thread sticker is, and standing down silently left the top
        rung of that person's record short by something that really happened.
        A hand-placed reaction needs nothing here -- the chance and the take
        were counted when this reaction was queued.

        The comment is fetched either way now, where the reply check used to
        short-circuit it: a queued reaction does not carry who wrote the
        comment (``scheduled`` has no column for it), and the message does.
        One extra read on the path where the operator answered by hand, which
        is the rare one.

        Fail-open by construction: an unreadable thread comes back empty and
        an unreadable comment comes back None, so the reaction still fires
        rather than wedging the queue -- and an unattributable reply is
        skipped without being recorded, never recorded against the wrong
        person.
        """
        if not self.deps.brain.params.skip_if_manually_replied:
            return False
        chat, comment_id = reaction.chat, reaction.reply_to
        history = await self.deps.account.history(
            chat, userchat.Slice(limit=MANUAL_REPLY_SCAN)
        )
        replied = any(m.out and m.reply_to == comment_id for m in history)
        comment = await self.deps.account.message(chat, comment_id)
        if not replied and not (comment is not None and comment.mine_reacted):
            return False
        if replied and comment is not None and comment.sender_id:
            self.deps.brain.recip_by_hand(comment.sender_id, comment_id)
        log.info('reaction: %s already answered by hand, skipping', comment_id)
        return True

    async def _deliver(self, reaction: reactions.Reaction) -> None:
        """Place the scheduled reaction: a like REACTION, or a thread STICKER.

        ``reaction.kind`` was decided at schedule time and stored, so the
        delivery
        is exactly what /status showed: 'react' puts a like reaction ON the
        comment; 'reply' sends the chosen premium reaction emoji as a message
        in the
        comment's thread (it reads like a sticker).
        """
        if reaction.kind == 'reply':
            await self._send_sticker(reaction)
        else:
            await self._send_reactions(reaction)

    async def _send_reactions(self, reaction: reactions.Reaction) -> None:
        """React to the commenter with the reaction(s) chosen at schedule.

        The reaction is placed ON the comment itself -- the reaction emoji
        shows as
        a reaction pill under the commenter's message, not as a reply in the
        thread. The emoji were picked when the comment was scheduled and stored
        on ``reaction``, so what lands is exactly what /status showed.
        """
        placed = await self.deps.account.react(
            reaction.chat, reaction.reply_to, reaction.emojis
        )
        if placed:
            glyphs = ''.join(fb for _, fb in reaction.emojis)
            log.info(
                'reaction: reacted %s on comment %s in %s',
                glyphs,
                reaction.reply_to,
                reaction.chat,
            )

    async def _send_sticker(self, reaction: reactions.Reaction) -> None:
        """Reply IN THE THREAD with the chosen premium emoji (a sticker).

        The emoji is sent as a message that replies to the comment inside its
        discussion thread (top=root), so it lands in the post's comments and
        reads like a sticker -- not a reaction pill. Falls back to a flat reply
        if the threaded send is refused (or it is a plain group), so a sticker
        is never lost to threading.
        """
        if not reaction.emojis:
            return
        emoji_id, fallback = reaction.emojis[0]
        message = RichText().emoji(Emoji(emoji_id, fallback)).build()
        threaded = bool(reaction.root) and reaction.root != reaction.reply_to
        text = userchat.Text(
            message.text,
            message.spans,
            reply_to=reaction.reply_to,
            thread=reaction.root if threaded else 0,
        )
        if threaded and await self.deps.account.send(reaction.chat, text):
            log.info(
                'reaction: sticker %s in thread %s of %s',
                fallback,
                reaction.root,
                reaction.chat,
            )
            return
        if threaded:
            log.warning(
                'reaction: threaded sticker failed in %s; flat', reaction.chat
            )
        await self.deps.account.send(reaction.chat, replace(text, thread=0))
        log.info(
            'reaction: sticker %s on comment %s in %s',
            fallback,
            reaction.reply_to,
            reaction.chat,
        )

    async def on_posted(self, target: int, post_id: int) -> None:
        """React to our own fresh post (optional), then watch its comments."""
        await self._react_to_post(target, post_id)
        await self._watch_post(target, post_id)

    async def _react_to_post(self, target: int, post_id: int) -> None:
        """Immediately place a reaction ON a freshly-posted message.

        Optional (``react_to_posts``, default off): reacting to our own posts
        is an extra, separate from the engine's real job of reacting to
        commenters. When on, there is no human-like wait -- the post is ours,
        so the reaction goes on straight away. A failure never blocks the
        post: the adapter logs it and answers False.
        """
        if not self.deps.brain.params.enabled or not post_id:
            return
        if not self.deps.brain.params.react_to_posts:
            return
        specs = self.deps.brain.pick_like(f'{target}:{post_id}')
        emojis = tuple((s.id, s.fallback) for s in specs)
        if await self.deps.account.react(target, post_id, emojis):
            glyphs = ''.join(fb for _, fb in emojis)
            log.info(
                'reaction: reacted %s to new post %s in %s',
                glyphs,
                post_id,
                target,
            )

    async def _watch_post(self, target: int, post_id: int) -> None:
        """Register where this post's comments appear (the react target).

        For a channel with a linked discussion (comments_in_discussion), the
        comments live in the discussion group: resolve the post's thread root
        and watch THAT, so reactions land only in the channel post's comments.
        For a
        plain group target, the post message id itself is the comment target.
        """
        if self.deps.brain.params.comments_in_discussion:
            # Channel: only watch a post whose discussion thread resolves; a
            # post with comments off (or deleted) adds nothing rather than a
            # useless channel-id entry that could evict a real one.
            thread = await self.deps.account.discussion_thread(target, post_id)
            if thread is not None:
                self.deps.brain.note_post(*thread)
            return
        self.deps.brain.note_post(target, post_id)
