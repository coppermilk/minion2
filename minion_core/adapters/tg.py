"""Telegram boundary: Bot API, long-poll, media receive (requests).

Sole importer of ``requests`` (REQ-ARC-002). Ref format (owned by
this adapter, opaque to the kernel): ``<chat>:<message>:<spool>`` --
the reply address plus the spooled copy's path; ``spool_of`` is the
DisposeSource locator for it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

from minion_core.adapters.files import BudgetWriter
from minion_core.adapters.files import next_free_path
from minion_core.adapters.files import sanitize
from minion_core.kernel import Envelope
from minion_core.kernel import Job
from minion_core.kernel import Origin
from minion_core.kernel import Source
from minion_core.kernel import atomic_write

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterator
    from collections.abc import Mapping

    from minion_core.kernel import Emit

_LOG = logging.getLogger('tg')

POLL_TIMEOUT_SEC = 25
"""Long-poll wait passed to getUpdates."""

API_TIMEOUT_SEC = 90
"""Wall-time bound on every HTTP call (bounded, BLUEPRINT 10)."""

CHUNK = 64 * 1024
"""Streaming download chunk size."""

_URL = re.compile(r'https?://\S+')


def _scrub(text: str, token: str) -> str:
    """Redact the bot token from a message before it is surfaced.

    ``requests`` puts the full request URL -- which embeds
    ``/bot<TOKEN>/`` -- into its exception messages, and those
    messages reach the log (source_crashed) and any reply. A leaked
    token is a full account takeover, so it never leaves this module
    in the clear; the log keeps the method and status, not the secret.
    """
    return text.replace(token, '<token>') if token else text


class TgError(Exception):
    """The Bot API refused a call."""


@dataclass(frozen=True)
class TgApi:
    """Bot API endpoint; an empty token means transport absent."""

    token: str
    base: str = 'https://api.telegram.org'

    @property
    def live(self) -> bool:
        """Whether the transport is configured."""
        return bool(self.token)

    def call(self, method: str, params: dict[str, Any]) -> Any:  # noqa: ANN401 -- Bot API returns free-form JSON
        """POST one Bot API method; raise TgError on refusal."""
        import requests

        url = f'{self.base}/bot{self.token}/{method}'
        try:
            resp = requests.post(url, json=params, timeout=API_TIMEOUT_SEC)
            body = resp.json()
        except requests.RequestException as exc:
            # `from None`: break the chain so the original exception --
            # whose message embeds the token-bearing URL -- cannot reach
            # the log via the source thread's exception handler.
            clean = _scrub(str(exc), self.token)
            raise TgError(f'{method}: {clean}') from None
        if not body.get('ok'):
            raise TgError(f'{method}: {body.get("description")}')
        return body['result']

    def download(
        self, file_id: str, spool: SpoolSpec, name: str | None = None
    ) -> Path:
        """Stream a file by id into the spool, budget-bounded.

        ``name`` is the sender's original filename
        (``document.file_name``); it is preserved verbatim
        (sanitized, not replaced) so meaningful names survive. Only
        when Telegram gives no name do we fall back to the opaque
        server basename.
        """
        import requests

        meta = self.call('getFile', {'file_id': file_id})
        remote = meta['file_path']
        base = name or remote.rsplit('/', 1)[-1]
        target = next_free_path(spool.into / sanitize(base))
        url = f'{self.base}/file/bot{self.token}/{remote}'
        try:
            with requests.get(
                url, stream=True, timeout=API_TIMEOUT_SEC
            ) as resp:
                resp.raise_for_status()
                chunks = resp.iter_content(CHUNK)
                return _spool(chunks, target, spool.budget())
        except requests.RequestException as exc:
            raise TgError(f'getFile: {_scrub(str(exc), self.token)}') from None


@dataclass(frozen=True)
class SpoolSpec:
    """Where downloads land and how many bytes they may take.

    ``budget`` is read per download so the mid-stream quota check
    (REQ-RES-002) tracks the live tree, not a stale snapshot.
    """

    into: Path
    budget: Callable[[], int]


def _spool(chunks: Iterator[bytes], target: Path, budget: int) -> Path:
    """Write a chunk stream under a byte budget (REQ-RES-002)."""
    writer = BudgetWriter(target, budget)
    try:
        for chunk in chunks:
            writer.write(chunk)
    except BaseException:
        writer.abort()
        raise
    return writer.commit()


class OffsetStore:
    """Telegram high-water mark on disk (STATE; REQ-DATA-003)."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> int:
        """Read the offset; a lost offset is loud, then replays."""
        try:
            return int(self._path.read_text(encoding='ascii'))
        except FileNotFoundError:
            return 0
        except (OSError, ValueError):
            _LOG.warning('offset_lost path=%s', self._path)
            return 0

    def write(self, offset: int) -> None:
        """Persist the offset atomically (REQ-DATA-002)."""
        atomic_write(self._path, str(offset).encode('ascii'))


class TgChannel:
    """A bot's Telegram identity; a no-op without a token.

    Loss of the transport degrades the bot to folder-only with zero
    caller branching (REQ-DEG-001).
    """

    def __init__(self, api: TgApi) -> None:
        self._api = api

    def send_text(self, origin: Origin, text: str) -> None:
        """Reply toward the origin chat; no-op when tokenless."""
        if not self._api.live or ':' not in origin.ref:
            return
        self._api.call('sendMessage', {'chat_id': _chat(origin), 'text': text})

    def edit_text(self, origin: Origin, text: str) -> None:
        """Edit the job's ack message in place; no-op if there is none.

        The seam for one self-editing status message: the belt moves the
        same message through downloading/sending/done/error instead of
        sending a new one each time. No ack id (or no token) -> no-op.
        """
        ack = _ack_id(origin)
        if not self._api.live or ack is None:
            return
        self._api.call(
            'editMessageText',
            {'chat_id': _chat(origin), 'message_id': int(ack), 'text': text},
        )

    def send_file(self, origin: Origin, path: Path) -> None:
        """Upload toward the origin chat, always as a document.

        Results go back as documents -- never recompressed by
        Telegram (the documents-only contract, both directions).
        """
        if not self._api.live or ':' not in origin.ref:
            return
        import requests

        url = f'{self._api.base}/bot{self._api.token}/sendDocument'
        try:
            with path.open('rb') as fh:
                resp = requests.post(
                    url,
                    data={'chat_id': _chat(origin)},
                    files={'document': (path.name, fh)},
                    timeout=API_TIMEOUT_SEC,
                )
            ok = resp.json().get('ok')
        except requests.RequestException as exc:
            raise TgError(
                f'sendDocument: {_scrub(str(exc), self._api.token)}'
            ) from None
        if not ok:
            raise TgError(f'sendDocument: {path.name}')


def _chat(origin: Origin) -> str:
    """The chat part of a ``chat:message:ack:spool`` origin ref."""
    return origin.ref.split(':', 1)[0]


_REF_PARTS = 4
"""chat : message : ack : spool -- the four fields of a tg ref."""


def _ack_id(origin: Origin) -> str | None:
    """The ack message id in a tg ref, or None when there is none."""
    parts = origin.ref.split(':', _REF_PARTS - 1)
    return parts[2] if len(parts) >= _REF_PARTS - 1 and parts[2] else None


def spool_of(origin: Origin) -> Path | None:
    """DisposeSource locator: the spool part of a tg ref."""
    parts = origin.ref.split(':', _REF_PARTS - 1)
    if len(parts) < _REF_PARTS or not parts[3]:
        return None
    return Path(parts[3])


def spooled_or_dropped(origin: Origin) -> Path | None:
    """The disposable input file for either transport.

    A Telegram job carries its spool inside the ref (``spool_of``); a
    folder drop is a ``loc`` origin whose ref IS the dropped file. Used
    by Shelve so a dropped original is filed beside its output just like
    a Telegram one, rather than lingering in the drop folder.
    """
    if origin.source == 'tg':
        return spool_of(origin)
    return Path(origin.ref) if origin.ref else None


def chats_from(env: Mapping[str, str]) -> tuple[str, ...]:
    """The chat allow-list (primary control, OPERATIONS 3)."""
    raw = env.get('TG_CHATS', '')
    return tuple(part.strip() for part in raw.split(',') if part.strip())


def chat_title(api: TgApi, chat: str) -> str:
    """A human name for a chat id via getChat, or '' when unavailable.

    A group/channel resolves to its title, a user to a username or first
    name. A refusal (unknown chat, no access, tokenless) is '', never a
    crash -- the caller shows the raw id instead.
    """
    if not api.live or not chat:
        return ''
    try:
        info = api.call('getChat', {'chat_id': chat})
    except TgError:
        return ''
    return _chat_name(info)


def _chat_name(info: object) -> str:
    """The first present name field of a getChat result, or ''."""
    if not isinstance(info, dict):
        return ''
    for key in ('title', 'username', 'first_name'):
        value = info.get(key)
        if isinstance(value, str) and value:
            return value
    return ''


DOCS_ONLY = 'Send files as a document (not a compressed photo/video).'
"""The documents-only reminder appended to every bot's help line."""

NO_ACCESS = 'Sorry, you are not authorized to use this bot.'
"""Reply to a direct message from a chat outside the allow-list."""


@dataclass(frozen=True)
class TgSpec:
    """Where a Telegram dock spools, delivers, and remembers."""

    spool: SpoolSpec
    dest: Path
    offset: Path
    chats: tuple[str, ...]
    help: str = ''
    ack: str = ''
    """Sent the moment a work message is seen (before the download). Empty
    disables it; the caller sets the text (the relay keys it per bot)."""
    parse_mode: str = ''
    """Telegram parse mode for command replies (e.g. ``HTML``). Empty is
    plain text; a command dock that returns markup (the moderator's
    tables) sets it so the reply renders."""


class _TgSource(Source):
    """Long-poll dock; persists the offset per update (CT-A)."""

    def __init__(self, api: TgApi, spec: TgSpec) -> None:
        super().__init__()
        self.api = api
        self.spec = spec
        self._offsets = OffsetStore(spec.offset)

    def produce(self, emit: Emit) -> None:
        """Drain getUpdates forever; tokenless ends immediately."""
        if not self.api.live:
            _LOG.info('tokenless: folder-only degradation')
            return
        offset = self._offsets.read()
        while not self.stopped:
            offset = self._poll(offset, emit)

    def _poll(self, offset: int, emit: Emit) -> int:
        updates = self.api.call(
            'getUpdates',
            {'offset': offset, 'timeout': POLL_TIMEOUT_SEC},
        )
        for upd in updates:
            offset = max(offset, self._consume(upd, emit))
        return offset

    def _consume(self, upd: dict[str, Any], emit: Emit) -> int:
        """Validate one untrusted update explicitly (BLUEPRINT 4).

        A malformed payload is a logged ``bad_update``, never a
        crashed dock; the offset still advances past it, so a poison
        update cannot wedge the bot in a replay loop.
        """
        uid = upd.get('update_id')
        if not isinstance(uid, int):
            _LOG.warning('rejected reason=bad_update keys=%s', sorted(upd))
            return 0
        try:
            self._route(upd, emit)
        except (KeyError, TypeError, ValueError) as exc:
            _LOG.warning('rejected reason=bad_update id=%s err=%s', uid, exc)
        self._offsets.write(uid + 1)  # REQ-DATA-003
        return uid + 1

    def _route(self, upd: dict[str, Any], emit: Emit) -> None:
        msg = upd.get('message')
        if not msg:
            return
        chat = str(msg['chat']['id'])
        if chat not in self.spec.chats:
            _LOG.warning('rejected reason=chat_not_allowed chat=%s', chat)
            self._deny(msg)
            return
        self.accept(msg, emit)

    def _deny(self, msg: dict[str, Any]) -> None:
        """Tell a private sender outside the allow-list they lack access.

        Only in a 1:1 chat: replying to every message in an unauthorized
        group would spam it (and risk a Telegram ban), so a group is
        still rejected in silence -- only the person who DMs the bot
        directly is told. Tokenless docks stay silent (REQ-DEG-001).
        """
        if not self.api.live:
            return
        if msg.get('chat', {}).get('type') != 'private':
            return
        self.api.call(
            'sendMessage', {'chat_id': msg['chat']['id'], 'text': NO_ACCESS}
        )

    def accept(self, msg: dict[str, Any], emit: Emit) -> None:
        """Turn one allowed message into envelopes (per source)."""
        raise NotImplementedError

    def offer_help(self, msg: dict[str, Any]) -> None:
        """Reply a one-line usage hint when a message produced no work.

        A plain text or a compressed photo/video gets a friendly
        nudge instead of silence: what this bot does, plus the
        documents-only reminder.
        """
        if not self.api.live or not self.spec.help:
            return
        text = f'{self.spec.help} {DOCS_ONLY}'
        self.api.call(
            'sendMessage', {'chat_id': msg['chat']['id'], 'text': text}
        )

    def announce_start(self, msg: dict[str, Any]) -> int | None:
        """Ack a work message at once; return the ack's message id.

        The sender learns the task began before any slow download. The
        returned id lets the belt EDIT this one message through the task's
        life (downloading %, sending, done/error) instead of piling up new
        messages. Empty ``spec.ack`` or no transport -> None.
        """
        if not self.api.live or not self.spec.ack:
            return None
        result = self.api.call(
            'sendMessage',
            {'chat_id': msg['chat']['id'], 'text': self.spec.ack},
        )
        got = result.get('message_id') if isinstance(result, dict) else None
        return got if isinstance(got, int) else None

    def emit_spooled(
        self, spooled: Path, ctx: MsgCtx, ack: int | None
    ) -> None:
        """Emit a job addressed to the chat, disposing the spool.

        The ack message id rides in the ref so the belt can edit that one
        message as the task progresses (``_ack_id``).
        """
        ref = f'{_ref(ctx.msg)}:{ack if ack is not None else ""}:{spooled}'
        job = Job(
            src=spooled,
            dest=self.spec.dest,
            stem=spooled.stem,
            origin=Origin('tg', ref),
        )
        ctx.emit(Envelope(job))


@dataclass(frozen=True)
class MsgCtx:
    """One accepted message plus the belt it feeds."""

    msg: dict[str, Any]
    emit: Emit


def _ref(msg: dict[str, Any]) -> str:
    return f'{msg["chat"]["id"]}:{msg["message_id"]}'


def _accept_media(src: _TgSource, ctx: MsgCtx) -> bool:
    """Spool a document payload; True when one was emitted."""
    doc = _document(ctx.msg)
    if doc is None:
        return False
    ack = src.announce_start(ctx.msg)  # ack before the (maybe slow) download
    got = src.api.download(
        str(doc['file_id']), src.spec.spool, doc.get('file_name')
    )
    src.emit_spooled(got, ctx, ack)
    return True


def _accept_link(src: _TgSource, ctx: MsgCtx) -> bool:
    """Spool the first link as a .url file; True when emitted."""
    text = ctx.msg.get('text', '') or ctx.msg.get('caption', '')
    match = _URL.search(text)
    if match is None:
        return False
    ack = src.announce_start(ctx.msg)  # ack before the (maybe slow) fetch
    url = match.group(0)
    name = sanitize(url.rsplit('/', 1)[-1] or 'link') + '.url'
    spooled = next_free_path(src.spec.spool.into / name)
    atomic_write(spooled, url.encode('ascii', 'replace'))
    src.emit_spooled(spooled, ctx, ack)
    return True


class TgMedia(_TgSource):
    """Emit document payloads, spooled to disk (documents only)."""

    def accept(self, msg: dict[str, Any], emit: Emit) -> None:
        """Download the message payload and emit its job, or help."""
        if not _accept_media(self, MsgCtx(msg, emit)):
            self.offer_help(msg)


_COMPRESSED = ('photo', 'video', 'video_note', 'animation')
"""Payload kinds Telegram recompresses; the contract refuses them."""


def _document(msg: dict[str, Any]) -> dict[str, Any] | None:
    """The document payload, if any (carries file_id + file_name).

    Files cross Telegram as documents only, both directions:
    compressed photo/video payloads are refused loudly so the sender
    learns to re-send as a file, and originals stay originals.
    """
    doc = msg.get('document')
    if isinstance(doc, dict):
        return doc
    if any(msg.get(kind) for kind in _COMPRESSED):
        _LOG.warning(
            'rejected reason=not_a_document chat=%s', msg['chat']['id']
        )
    return None


class TgLinks(_TgSource):
    """Emit the first link of each message as a spooled .url file."""

    def accept(self, msg: dict[str, Any], emit: Emit) -> None:
        """Spool the link and emit its job, or reply with help."""
        if not _accept_link(self, MsgCtx(msg, emit)):
            self.offer_help(msg)


class TgAny(_TgSource):
    """Emit links and media payloads from one long-poll dock.

    One token allows one getUpdates consumer, so a bot wanting both
    kinds runs one dock, never a merge of two Telegram docks.
    """

    def accept(self, msg: dict[str, Any], emit: Emit) -> None:
        """Prefer the link; fall through to the payload, else help."""
        ctx = MsgCtx(msg, emit)
        if not _accept_link(self, ctx) and not _accept_media(self, ctx):
            self.offer_help(msg)


class TgCommands(_TgSource):
    """A text-command dock: answer each message, emit no belt jobs.

    For control bots (moderator) and query bots (props) that reply
    in chat rather than feeding the belt. The injected ``handle`` maps
    the message text to a reply string; an empty reply stays silent.
    Reuses the long-poll, offset and chat-allowlist machinery of the
    base source.
    """

    def __init__(
        self, api: TgApi, spec: TgSpec, handle: Callable[[str], str]
    ) -> None:
        super().__init__(api, spec)
        self._handle = handle

    def accept(self, msg: dict[str, Any], _emit: Emit) -> None:
        """Answer the message text; no envelope is produced."""
        text = msg.get('text', '') or msg.get('caption', '')
        reply = self._handle(text)
        if not reply:
            return
        params: dict[str, Any] = {'chat_id': msg['chat']['id'], 'text': reply}
        if self.spec.parse_mode:
            params['parse_mode'] = self.spec.parse_mode
        self.api.call('sendMessage', params)


class TgPublicCommands(TgCommands):
    """A command dock open to any chat -- no allow-list (public commands).

    A public trigger anyone may send is answered in whatever chat it
    arrived from; the injected ``handle`` returns '' for everything else,
    so ordinary chatter stays silent and only the trigger gets a reply.
    """

    def _route(self, upd: dict[str, Any], emit: Emit) -> None:
        """Answer every chat's message; the allow-list is bypassed."""
        msg = upd.get('message')
        if msg:
            self.accept(msg, emit)
