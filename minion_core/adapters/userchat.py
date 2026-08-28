# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Telegram boundary: a real USER account over MTProto (telethon).

Sole importer of ``telethon`` (REQ-ARC-002). Everything above this file
deals in the value types below -- ``Msg``, ``Peer``, ``Story`` -- instead of
duck-typing Telethon objects with ``getattr``, where a mistyped field name
silently yields ``None`` and the bot quietly misbehaves.

Two things follow from being the only door:

* **Pacing cannot be bypassed.** Every method waits on one ``Gate`` first,
  so the account has a single, enforceable request rate rather than four
  per-feature throttles and open season everywhere else.
* **The failure mode lives here.** A method returns ``None`` / ``[]`` /
  ``False`` when Telegram is unreachable and says so in the log. Callers
  keep only the failures that are POLICY -- "the operator already replied
  by hand", "the post did not go out, re-queue it" -- which belong in
  sight, not buried in a transport wrapper.

The vendor is imported inside the functions that use it: ``telethon`` is
the optional ``tg`` extra, and this module must import without it so the
rest of the tree (and the test suite) does not depend on it.

**Porting to another platform.** The behaviour above this file is already
platform-free: ``core/attachment``, ``core/relationship``, ``core/humanize``,
``core/state`` and ``engines/reactions`` mention Telegram nowhere in their
code. They answer "engage this person now, with this glyph, as a like or as
the stronger act", which is a statement about a relationship, not about a
protocol. A second platform reuses them untouched, gets its own rows in the
store for free (the peer table is keyed by engine name), and needs two
things of its own: a module like this one, and the glue that knows WHERE a
comment lives -- a discussion thread here, media comments elsewhere. No
Protocol is declared yet, on purpose (Power of 10, rule 9: the port ships
the day the second provider does); the surface below is kept neutral so
that day is an extraction, not a rewrite.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from minion_core.pace import Gate
from minion_core.pace import Lane
from minion_core.pace import Pace
from minion_core.richtext import EMOJI
from minion_core.richtext import LINK
from minion_core.richtext import UNDERLINE
from minion_core.richtext import Span

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from collections.abc import Sequence
    from datetime import datetime
    from pathlib import Path

    from telethon import TelegramClient


_LOG = logging.getLogger('userchat')

READ = 'read'
THREAD = 'thread'
WRITE = 'write'
REACT = 'react'
STORY = 'story'
DM = 'dm'
PROBE = 'probe'
"""The request kinds the gate paces separately."""

SLOW_CALL_SEC = 5.0
"""A call slower than this is read as a flood Telethon absorbed for us.

Telethon sleeps off a FloodWait under its ``flood_sleep_threshold`` and
returns normally, so the adapter never sees the error for the common case.
It does see the latency: an MTProto round trip is well under a second, so
seconds of it means something upstream throttled us, and the gate should
widen even though nothing raised.
"""

DEFAULT_FLOOD_SLEEP = 3600.0
"""Sleep off a FloodWait up to this long rather than raising.

The single most account-friendly behaviour under sustained automation: on a
FloodWait the client waits it out and retries instead of erroring. A flood
is per request and each engine is its own task, so one sleeping call never
blocks the others or the liveness probe.
"""


@dataclass(frozen=True)
class Peer:
    """A chat or user, reduced to what this project ever reads off one."""

    id: int
    username: str = ''
    first_name: str = ''
    last_name: str = ''
    phone: str = ''
    title: str = ''


@dataclass(frozen=True)
class Msg:
    """One message, reduced to what this project ever reads off one.

    ``root`` is the post a comment hangs under, and the reason the field
    exists: a comment on a channel post is a reply in the discussion group,
    where a NESTED comment carries ``reply_to_top_id`` but a FIRST-LEVEL
    one carries only ``reply_to_msg_id`` -- the same root, under another
    name. Reading just the first would leave every top-level comment
    rooted at 0, which is to say invisible to the engine that answers
    them. ``reply_to`` is the message it answers directly; ``root`` is 0
    when the message is not a reply at all.
    """

    id: int
    chat_id: int = 0
    text: str = ''
    out: bool = False
    sender_id: int = 0
    reply_to: int = 0
    root: int = 0
    date: datetime | None = None
    mine_reacted: bool = False
    spans: tuple[Span, ...] = ()


@dataclass(frozen=True)
class Story:
    """One active story of a peer."""

    id: int
    date: float = 0.0


@dataclass(frozen=True)
class PeerStories:
    """One peer's active stories, as the feed reports them."""

    peer_id: int
    stories: tuple[Story, ...] = ()


@dataclass(frozen=True)
class MemberEvent:
    """One join/leave from a channel's admin log."""

    id: int
    user_id: int
    joined: bool = False
    left: bool = False


@dataclass(frozen=True)
class Slice:
    """Which slice of a chat's history to read.

    ``mine`` restricts to our own messages (finding our posts in a channel);
    ``under`` reads a discussion thread; ``ids`` fetches exact messages.
    """

    limit: int = 1
    under: int = 0
    ids: int = 0
    mine: bool = False


@dataclass(frozen=True)
class Text:
    """A message to send, with however it wants to be formatted.

    ``spans`` carries premium-emoji formatting built upstream; ``html``
    is the alternative for templates that ship markup. ``thread`` makes the
    reply land inside a discussion thread rather than flat.
    """

    body: str
    spans: Sequence[Span] = ()
    reply_to: int = 0
    thread: int = 0
    html: bool = False
    preview: bool = False


def paces(runtime: dict[str, object]) -> Gate:
    """Build the request gate from the constants JSON ``runtime`` block.

    The defaults are deliberately looser than the engines' own cadences --
    the gate exists to cut BURSTS (a backfill, a rescan, a status render
    resolving every peer at once), not to slow the human-paced work down.
    """

    def num(key: str, fallback: float) -> float:
        raw = runtime.get(key, fallback)
        return float(raw) if isinstance(raw, (int, float)) else fallback

    return Gate(
        {
            READ: Pace(num('read_gap_sec', 1.0), int(num('read_per_min', 30))),
            # Resolving a post's comment thread is the call that actually
            # trips Telegram: it fires in bursts (every watched post, on
            # every startup and rescan), so it gets its own slower lane.
            THREAD: Pace(
                num('thread_gap_sec', 2.0), int(num('thread_per_min', 20))
            ),
            WRITE: Pace(
                num('write_gap_sec', 3.0), int(num('write_per_min', 15))
            ),
            REACT: Pace(
                num('react_gap_sec', 2.0), int(num('react_per_min', 20))
            ),
            STORY: Pace(
                num('story_gap_sec', 1.0), int(num('story_per_min', 30))
            ),
            DM: Pace(num('dm_gap_sec', 45.0), int(num('dm_per_min', 2))),
            PROBE: Pace(num('probe_gap_sec', 60.0)),
        },
        overall=Pace(
            num('overall_gap_sec', 0.5), int(num('overall_per_min', 60))
        ),
    )


@dataclass
class Account:
    """One Telegram user account, paced and typed.

    ``client`` is a Telethon ``TelegramClient``; nothing above this module
    touches it. Every method takes and returns the value types above, so
    the surface says nothing about MTProto that a second platform could not
    also say.
    """

    client: TelegramClient
    gate: Gate = field(default_factory=lambda: paces({}))

    # --- incoming --------------------------------------------------------

    def on_message(self, handler: Callable[[Msg], Awaitable[None]]) -> None:
        """Deliver every incoming message to ``handler`` as a typed ``Msg``.

        The other half of the door, and the leakier one: an incoming
        Telethon event used to travel three layers deep -- the host read
        ``raw_text`` off it, then handed the same object to the comment
        watcher and the audience log, each of which duck-typed its own
        fields again. Converting once, here, is what makes the layers
        above able to state what they receive.
        """
        from telethon import events

        async def deliver(event: object) -> None:
            await handler(_msg(getattr(event, 'message', event)))

        self.client.add_event_handler(deliver, events.NewMessage())

    # --- reads -----------------------------------------------------------

    async def me(self) -> Peer | None:
        """Return our own account, or None when the link is wedged."""
        got = await self._call(PROBE, self.client.get_me())
        return _peer(got) if got is not None else None

    async def peer(self, chat: int) -> Peer | None:
        """Resolve a chat or user, or None when it cannot be reached."""
        got = await self._call(READ, self.client.get_entity(chat))
        return _peer(got) if got is not None else None

    async def history(self, chat: int, span: Slice) -> list[Msg]:
        """Read a slice of a chat's history, newest first ([] on failure)."""
        kwargs: dict[str, object] = {'limit': span.limit}
        if span.under:
            kwargs['reply_to'] = span.under
        if span.ids:
            kwargs['ids'] = span.ids
        if span.mine:
            kwargs['from_user'] = 'me'
        got = await self._call(READ, self.client.get_messages(chat, **kwargs))
        if got is None:
            return []
        rows = got if isinstance(got, list) else [got]
        return [_msg(row) for row in rows if row is not None]

    async def message(self, chat: int, msg_id: int) -> Msg | None:
        """Read one message by id, or None when it is gone."""
        rows = await self.history(chat, Slice(ids=msg_id))
        return rows[0] if rows else None

    async def discussion_thread(
        self, channel: int, post_id: int
    ) -> tuple[int, int] | None:
        """(discussion chat, thread root) for a channel post, or None.

        None when the post has comments turned off, or the channel has no
        linked group -- in which case there is nothing to watch.
        """
        from telethon.tl.functions.messages import GetDiscussionMessageRequest

        got = await self._call(
            THREAD, self.client(GetDiscussionMessageRequest(channel, post_id))
        )
        rows = getattr(got, 'messages', None) or []
        if not rows:
            return None
        chat_id = int(getattr(rows[0], 'chat_id', 0) or 0)
        root_id = int(getattr(rows[0], 'id', 0) or 0)
        return (chat_id, root_id) if chat_id and root_id else None

    async def admin_log(
        self, channel: int, after: int
    ) -> list[MemberEvent] | None:
        """Join/leave events newer than ``after``; None when unreadable.

        The one read where empty and unreadable must not look alike: the
        greeter takes its FIRST successful read as the baseline it will
        never greet, so an unreadable log answering [] would silently
        baseline the account past everyone who joined before it.
        """
        await self.gate.wait(READ)
        try:
            return [
                _event(row)
                async for row in self.client.iter_admin_log(
                    channel, min_id=after, join=True, leave=True
                )
            ]
        except Exception:  # noqa: BLE001 -- not admin / unreachable: skip
            _LOG.warning('admin log of %s unreadable (admin?)', channel)
            return None

    # --- writes ----------------------------------------------------------

    async def send(self, chat: int, text: Text) -> int:
        """Send a message; return its id, or 0 when it did not go out."""
        if text.thread:
            return await self._send_threaded(chat, text)
        sent = await self._call(
            WRITE,
            self.client.send_message(chat, text.body, **_send_kwargs(text)),
        )
        return _sent_id(sent)

    async def send_photo(
        self, chat: int, photo: str | Path, text: Text
    ) -> int:
        """Send a photo with a caption; 0 when it did not go out.

        ``photo`` is a local file or a URL Telegram fetches itself -- the
        aggregator posts a YouTube thumbnail by url. It stays a str here
        rather than a Path because Path() collapses the // in a url.
        """
        sent = await self._call(
            WRITE,
            self.client.send_file(
                chat,
                str(photo),
                caption=text.body,
                **_caption_kwargs(text),
            ),
        )
        return _sent_id(sent)

    async def dm(self, user_id: int, text: Text) -> bool:
        """Send a direct message; False when it was refused or capped.

        Paced apart from the rest: DMs from a user account are the top
        trigger for a spam ban, so they get their own, far slower lane.
        """
        sent = await self._call(
            DM,
            self.client.send_message(user_id, text.body, **_send_kwargs(text)),
        )
        return sent is not None

    def pacing(self) -> list[Lane]:
        """Report every lane of the gate, for an operator readout."""
        return self.gate.lanes()

    def strained(self, kind: str) -> bool:
        """Whether Telegram recently told us to slow this lane down.

        The gate widens a lane on a FloodWait and eases back after clean
        requests, so a widened lane IS the "we are being throttled" signal
        -- which the DM path needs, because carrying on DMing straight
        after a flood is how a user account earns a spam ban.
        """
        return self.gate.slack(kind) > 1.0

    async def _send_threaded(self, chat: int, text: Text) -> int:
        """Reply inside a discussion thread, so it lands in the comments."""
        from telethon.tl.functions.messages import SendMessageRequest
        from telethon.tl.types import InputReplyToMessage

        sent = await self._call(
            WRITE,
            self.client(
                SendMessageRequest(
                    peer=chat,
                    message=text.body,
                    entities=entities(text.body, text.spans) or None,
                    reply_to=InputReplyToMessage(
                        reply_to_msg_id=text.reply_to, top_msg_id=text.thread
                    ),
                    no_webpage=not text.preview,
                )
            ),
        )
        return _sent_id(sent)

    # --- reactions -------------------------------------------------------

    async def react(
        self, chat: int, msg_id: int, emojis: Sequence[tuple[str, str]]
    ) -> bool:
        """Place premium reactions on a message, falling back to plain ones.

        Reactions are atomic -- one request carries the account's whole set
        on that message. A chat that forbids custom emoji (or an account
        that is not Premium) gets the fallback glyphs instead, so something
        still lands.
        """
        from telethon.tl.types import ReactionCustomEmoji
        from telethon.tl.types import ReactionEmoji

        if not emojis:
            return False
        custom = [ReactionCustomEmoji(document_id=int(i)) for i, _ in emojis]
        if await self._react(chat, msg_id, custom):
            return True
        plain = [ReactionEmoji(emoticon=fb) for _, fb in emojis]
        placed = await self._react(chat, msg_id, plain)
        if placed:
            _LOG.info('custom reaction refused in %s; used plain', chat)
        return placed

    async def _react(
        self, chat: int, msg_id: int, reaction: list[object]
    ) -> bool:
        """One SendReaction call; False when Telegram refused it."""
        from telethon.tl.functions.messages import SendReactionRequest

        got = await self._call(
            REACT,
            self.client(
                SendReactionRequest(
                    peer=chat,
                    msg_id=msg_id,
                    reaction=reaction,
                    add_to_recent=True,
                )
            ),
            quiet=True,
        )
        return got is not None

    # --- stories ---------------------------------------------------------

    async def stories_feed(self, *, hidden: bool = False) -> list[PeerStories]:
        """Read the active-stories feed (contacts / followed peers only)."""
        from telethon.tl.functions.stories import GetAllStoriesRequest

        got = await self._call(
            STORY, self.client(GetAllStoriesRequest(hidden=hidden))
        )
        rows = getattr(got, 'peer_stories', None) or []
        found = [_peer_stories(row) for row in rows]
        return [row for row in found if row is not None]

    async def view_story(self, peer: object, story_id: int) -> bool:
        """Register one story view (best effort; read_stories is the mark)."""
        from telethon.tl.functions.stories import IncrementStoryViewsRequest

        got = await self._call(
            STORY,
            self.client(IncrementStoryViewsRequest(peer=peer, id=[story_id])),
            quiet=True,
        )
        return got is not None

    async def read_stories(self, peer: object, max_id: int) -> bool:
        """Mark a peer's stories read up to ``max_id``."""
        from telethon.tl.functions.stories import ReadStoriesRequest

        got = await self._call(
            STORY, self.client(ReadStoriesRequest(peer=peer, max_id=max_id))
        )
        return got is not None

    async def react_to_story(
        self, peer: object, story_id: int, emoji: str
    ) -> bool:
        """Leave one reaction on a story; False when it did not land."""
        from telethon.tl.functions.stories import SendReactionRequest
        from telethon.tl.types import ReactionEmoji

        got = await self._call(
            REACT,
            self.client(
                SendReactionRequest(
                    peer=peer,
                    story_id=story_id,
                    reaction=ReactionEmoji(emoticon=emoji),
                )
            ),
            quiet=True,
        )
        return got is not None

    async def input_peer(self, peer_id: int) -> object | None:
        """Resolve a peer to the handle the story requests want."""
        return await self._call(READ, self.client.get_input_entity(peer_id))

    # --- the one place a request actually leaves ------------------------

    async def _call(
        self, kind: str, work: Awaitable[object], *, quiet: bool = False
    ) -> object | None:
        """Wait for the gate, run one request, and account for what happened.

        Returns None on any failure -- that IS the adapter's contract, so
        callers deal in values rather than in Telethon exceptions. A slow
        call is treated as a flood Telethon already absorbed (see
        ``SLOW_CALL_SEC``) and widens the gate just as a raised one would.
        """
        await self.gate.wait(kind)
        started = time.monotonic()
        try:
            got = await work
        except Exception as exc:  # noqa: BLE001 -- the boundary: log, degrade
            if 'Flood' in type(exc).__name__:
                self.gate.flooded(kind)
                _LOG.warning('%s: flood wait, widening the gate', kind)
            elif not quiet:
                _LOG.warning(
                    '%s: request failed (%s)', kind, type(exc).__name__
                )
            return None
        if time.monotonic() - started > SLOW_CALL_SEC:
            self.gate.flooded(kind)
            _LOG.info('%s: slow round trip, widening the gate', kind)
        return got


_UTF16 = 'utf-16-le'
"""Telegram measures every entity offset in units of this encoding."""


def _units(text: str) -> int:
    """Length of ``text`` in UTF-16 code units -- Telegram's entity unit."""
    return len(text.encode(_UTF16)) // 2


def entities(text: str, spans: Sequence[Span]) -> list[object]:
    """Convert character-offset spans into Telegram message entities.

    THE conversion, and the reason the door is worth having. Telegram
    measures an entity in UTF-16 code units; everything above counts Python
    characters, and the two differ on exactly the non-BMP characters this
    account sends all day. Getting it wrong raises nothing -- it delivers a
    message whose colored glyph sits on the wrong letter -- so the
    arithmetic lives here once, under a test that pins the offsets.
    """
    from telethon.tl.types import MessageEntityCustomEmoji
    from telethon.tl.types import MessageEntityTextUrl
    from telethon.tl.types import MessageEntityUnderline

    out: list[object] = []
    for span in spans:
        at = _units(text[: span.at])
        size = _units(text[span.at : span.at + span.length])
        if span.kind == EMOJI:
            out.append(
                MessageEntityCustomEmoji(
                    offset=at, length=size, document_id=int(span.ref)
                )
            )
        elif span.kind == LINK:
            out.append(
                MessageEntityTextUrl(offset=at, length=size, url=span.ref)
            )
        else:
            out.append(MessageEntityUnderline(offset=at, length=size))
    return out


def _send_kwargs(text: Text) -> dict[str, object]:
    """Return the keyword arguments one Text implies for a plain send."""
    kwargs: dict[str, object] = {'link_preview': text.preview}
    if text.html:
        kwargs['parse_mode'] = 'html'
    elif text.spans:
        kwargs['formatting_entities'] = entities(text.body, text.spans)
    if text.reply_to:
        kwargs['reply_to'] = text.reply_to
    return kwargs


def _caption_kwargs(text: Text) -> dict[str, object]:
    """Return the keyword arguments a Text implies for a photo caption."""
    if text.html:
        return {'parse_mode': 'html'}
    return {'formatting_entities': entities(text.body, text.spans)}


def _sent_id(got: object) -> int:
    """Return the id of a message we just sent; 0 when it did not go out.

    A convenience send answers with the Message itself, but a raw request
    -- which is how a threaded reply has to be sent -- answers with an
    Updates envelope that has no ``id`` of its own; the new id rides
    inside it. Reading only the outer ``id`` made a SUCCESSFUL threaded
    send report 0, and 0 is the caller's signal that nothing landed, so a
    delivered sticker would have been sent a second time, flat.
    """
    direct = int(getattr(got, 'id', 0) or 0)
    if direct:
        return direct
    for update in getattr(got, 'updates', None) or []:
        inner = getattr(update, 'message', None)
        found = int(getattr(update, 'id', 0) or 0) or int(
            getattr(inner, 'id', 0) or 0
        )
        if found:
            return found
    return 0


def _peer(raw: object) -> Peer:
    """Read a Telethon entity into a Peer."""
    return Peer(
        id=int(getattr(raw, 'id', 0) or 0),
        username=str(getattr(raw, 'username', '') or ''),
        first_name=str(getattr(raw, 'first_name', '') or ''),
        last_name=str(getattr(raw, 'last_name', '') or ''),
        phone=str(getattr(raw, 'phone', '') or ''),
        title=str(getattr(raw, 'title', '') or ''),
    )


def _msg(raw: object) -> Msg:
    """Read a Telethon message into a Msg."""
    reply = getattr(raw, 'reply_to', None)
    return Msg(
        id=int(getattr(raw, 'id', 0) or 0),
        chat_id=int(getattr(raw, 'chat_id', 0) or 0),
        text=str(getattr(raw, 'message', '') or ''),
        out=bool(getattr(raw, 'out', False)),
        sender_id=int(getattr(raw, 'sender_id', 0) or 0),
        reply_to=int(getattr(reply, 'reply_to_msg_id', 0) or 0),
        root=_root(reply),
        date=getattr(raw, 'date', None),
        mine_reacted=_mine(getattr(raw, 'reactions', None)),
        spans=_spans(raw),
    )


def _root(reply: object) -> int:
    """Return the post id a reply hangs under (see ``Msg.root``); 0 if none."""
    top = getattr(reply, 'reply_to_top_id', None)
    if top is not None:
        return int(top)
    return int(getattr(reply, 'reply_to_msg_id', 0) or 0)


def _spans(raw: object) -> tuple[Span, ...]:
    """Read an incoming message's formatting back into neutral spans.

    The inverse of ``entities``: UTF-16 offsets in, characters out, same
    vocabulary. A door that only converts outgoing formatting is half a
    door -- the id of a premium emoji someone SENT us is exactly what the
    operator's dump tool is for, and it should not have to name the vendor
    to read one.
    """
    text = str(getattr(raw, 'message', '') or '')
    out: list[Span] = []
    for entity in getattr(raw, 'entities', None) or []:
        kind, ref = _mark(entity)
        if kind:
            at = _chars(text, 0, int(getattr(entity, 'offset', 0) or 0))
            size = _chars(
                text,
                int(getattr(entity, 'offset', 0) or 0),
                int(getattr(entity, 'length', 0) or 0),
            )
            out.append(Span(kind, at, size, ref))
    return tuple(out)


def _mark(entity: object) -> tuple[str, str]:
    """Return one entity's (kind, ref); ('', '') for one we do not model."""
    emoji = getattr(entity, 'document_id', None)
    if emoji is not None:
        return EMOJI, str(emoji)
    url = getattr(entity, 'url', None)
    if url is not None:
        return LINK, str(url)
    if type(entity).__name__.endswith('Underline'):
        return UNDERLINE, ''
    return '', ''


def _chars(text: str, offset: int, length: int) -> int:
    """Length in CHARACTERS of the UTF-16 slice [offset, offset+length).

    ``errors='ignore'`` covers an offset that would split a surrogate
    pair. Telegram never sends one; a malformed entity that did would
    otherwise raise here and take the whole history read down with it.
    """
    units = text.encode(_UTF16)
    piece = units[offset * 2 : (offset + length) * 2]
    return len(piece.decode(_UTF16, errors='ignore'))


def _mine(reactions: object) -> bool:
    """Whether THIS account already reacted to the message.

    The reliable signal is the tally: Telegram sets ``chosen_order`` on
    every result the current account picked, however many others reacted
    after. ``recent_reactions`` is a short, capacity-capped list, so it is
    only the fallback for older layers.
    """
    results = getattr(reactions, 'results', None) or []
    if any(getattr(r, 'chosen_order', None) is not None for r in results):
        return True
    recent = getattr(reactions, 'recent_reactions', None) or []
    return any(getattr(r, 'my', False) for r in recent)


def _peer_stories(raw: object) -> PeerStories | None:
    """Read one feed entry into a PeerStories, or None when it is empty."""
    from telethon import utils

    peer = getattr(raw, 'peer', None)
    items = getattr(raw, 'stories', None) or []
    stories = tuple(
        Story(id=sid, date=_epoch(getattr(item, 'date', None)))
        for item in items
        if (sid := int(getattr(item, 'id', 0) or 0)) > 0
    )
    if peer is None or not stories:
        return None
    return PeerStories(int(utils.get_peer_id(peer)), stories)


def _epoch(value: object) -> float:
    """Return a story's date as a unix timestamp, 0 when unreadable."""
    stamp = getattr(value, 'timestamp', None)
    if callable(stamp):
        return float(stamp())
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _event(raw: object) -> MemberEvent:
    """Read one admin-log row into a MemberEvent."""
    joined = bool(
        getattr(raw, 'joined', False) or getattr(raw, 'joined_invite', False)
    )
    return MemberEvent(
        id=int(getattr(raw, 'id', 0) or 0),
        user_id=int(getattr(raw, 'user_id', 0) or 0),
        joined=joined,
        left=bool(getattr(raw, 'left', False)),
    )


@dataclass(frozen=True)
class Login:
    """What it takes to open a session: where it lives and who we are.

    ``flood_sleep`` is the operator's knob for the behaviour described in
    ``connect`` -- how long a FloodWait is waited out rather than raised.
    """

    session: Path
    api_id: int
    api_hash: str
    flood_sleep: float = DEFAULT_FLOOD_SLEEP


def connect(login: Login) -> TelegramClient:
    """Build the client: gentle under floods, crash-safe session.

    Two account-friendliness measures live here because both entry points
    (the bot and the one-time login) need them:

    * ``flood_sleep_threshold`` well above Telethon's 60s default, so a
      FloodWait is waited out rather than raised (see the constant).
    * The SQLite session in WAL mode. The default DELETE journal is not
      crash-safe, and the watchdog turns a hang into a hard os._exit(1); a
      kill landing mid session-commit left the .session file corrupt
      ("database disk image is malformed") and forced a re-login every
      couple of weeks. WAL recovers on the next open; synchronous=NORMAL
      (safe under WAL) also trims fsync churn on the NAS.
    """
    from telethon import TelegramClient

    client = TelegramClient(
        str(login.session),
        login.api_id,
        login.api_hash,
        flood_sleep_threshold=login.flood_sleep,
    )
    _wal(client)
    return client


def _wal(client: TelegramClient) -> None:
    """Put the session's SQLite file into WAL mode, if it has one.

    Best effort: an in-memory session has no connection to harden, and a
    Telethon internal that moved just leaves the default journal in place
    -- a slower session file, never a broken run.
    """
    conn = getattr(getattr(client.session, '_conn', None), 'execute', None)
    if conn is None:
        _LOG.warning('session: no file to harden; default journal kept')
        return
    conn('PRAGMA journal_mode=WAL')
    conn('PRAGMA synchronous=NORMAL')
    _LOG.info('session: WAL journal enabled (crash-safe on hard exit)')
