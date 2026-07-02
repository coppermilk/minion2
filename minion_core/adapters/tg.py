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
        resp = requests.post(url, json=params, timeout=API_TIMEOUT_SEC)
        body = resp.json()
        if not body.get('ok'):
            raise TgError(f'{method}: {body.get("description")}')
        return body['result']

    def download(self, file_id: str, spool: SpoolSpec) -> Path:
        """Stream a file by id into the spool, budget-bounded."""
        import requests
        meta = self.call('getFile', {'file_id': file_id})
        remote = meta['file_path']
        name = sanitize(remote.rsplit('/', 1)[-1])
        target = next_free_path(spool.into / name)
        url = f'{self.base}/file/bot{self.token}/{remote}'
        with requests.get(url, stream=True,
                          timeout=API_TIMEOUT_SEC) as resp:
            resp.raise_for_status()
            chunks = resp.iter_content(CHUNK)
            return _spool(chunks, target, spool.budget())


@dataclass(frozen=True)
class SpoolSpec:
    """Where downloads land and how many bytes they may take.

    ``budget`` is read per download so the mid-stream quota check
    (REQ-RES-002) tracks the live tree, not a stale snapshot.
    """

    into: Path
    budget: Callable[[], int]


def _spool(chunks: Iterator[bytes], target: Path,
           budget: int) -> Path:
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
        self._api.call('sendMessage',
                       {'chat_id': _chat(origin), 'text': text})

    def send_file(self, origin: Origin, path: Path) -> None:
        """Upload a document toward the origin chat."""
        if not self._api.live or ':' not in origin.ref:
            return
        import requests
        url = f'{self._api.base}/bot{self._api.token}/sendDocument'
        with path.open('rb') as fh:
            resp = requests.post(
                url,
                data={'chat_id': _chat(origin)},
                files={'document': (path.name, fh)},
                timeout=API_TIMEOUT_SEC,
            )
        if not resp.json().get('ok'):
            raise TgError(f'sendDocument: {path.name}')


def _chat(origin: Origin) -> str:
    """The chat part of a ``chat:message:spool`` origin ref."""
    return origin.ref.split(':', 1)[0]


_REF_PARTS = 3
"""chat : message : spool -- the three fields of a tg ref."""


def spool_of(origin: Origin) -> Path | None:
    """DisposeSource locator: the spool part of a tg ref."""
    parts = origin.ref.split(':', _REF_PARTS - 1)
    if len(parts) < _REF_PARTS or not parts[2]:
        return None
    return Path(parts[2])


def chats_from(env: Mapping[str, str]) -> tuple[str, ...]:
    """The chat allow-list (primary control, OPERATIONS 3)."""
    raw = env.get('TG_CHATS', '')
    return tuple(part.strip() for part in raw.split(',') if part.strip())


@dataclass(frozen=True)
class TgSpec:
    """Where a Telegram dock spools, delivers, and remembers."""

    spool: SpoolSpec
    dest: Path
    offset: Path
    chats: tuple[str, ...]
    kinds: tuple[str, ...] = ('photo', 'video', 'document')


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
            self._route(upd, emit)
            offset = upd['update_id'] + 1
            self._offsets.write(offset)  # REQ-DATA-003
        return offset

    def _route(self, upd: dict[str, Any], emit: Emit) -> None:
        msg = upd.get('message')
        if not msg:
            return
        chat = str(msg['chat']['id'])
        if chat not in self.spec.chats:
            _LOG.warning('rejected reason=chat_not_allowed chat=%s',
                         chat)
            return
        self.accept(msg, emit)

    def accept(self, msg: dict[str, Any], emit: Emit) -> None:
        """Turn one allowed message into envelopes (per source)."""
        raise NotImplementedError

    def emit_spooled(self, spooled: Path, ctx: MsgCtx) -> None:
        """Emit a job addressed to the chat, disposing the spool."""
        ref = f'{_ref(ctx.msg)}:{spooled}'
        job = Job(src=spooled, dest=self.spec.dest,
                  stem=spooled.stem, origin=Origin('tg', ref))
        ctx.emit(Envelope(job))


@dataclass(frozen=True)
class MsgCtx:
    """One accepted message plus the belt it feeds."""

    msg: dict[str, Any]
    emit: Emit


def _ref(msg: dict[str, Any]) -> str:
    return f'{msg["chat"]["id"]}:{msg["message_id"]}'


def _accept_media(src: _TgSource, ctx: MsgCtx) -> bool:
    """Spool a matching payload; True when one was emitted."""
    file_id = _file_id(ctx.msg, src.spec.kinds)
    if file_id is None:
        return False
    got = src.api.download(file_id, src.spec.spool)
    src.emit_spooled(got, ctx)
    return True


def _accept_link(src: _TgSource, ctx: MsgCtx) -> bool:
    """Spool the first link as a .url file; True when emitted."""
    text = ctx.msg.get('text', '') or ctx.msg.get('caption', '')
    match = _URL.search(text)
    if match is None:
        return False
    url = match.group(0)
    name = sanitize(url.rsplit('/', 1)[-1] or 'link') + '.url'
    spooled = next_free_path(src.spec.spool.into / name)
    atomic_write(spooled, url.encode('ascii', 'replace'))
    src.emit_spooled(spooled, ctx)
    return True


class TgMedia(_TgSource):
    """Emit photo/video/document payloads, spooled to disk."""

    def accept(self, msg: dict[str, Any], emit: Emit) -> None:
        """Download the message payload and emit its job."""
        _accept_media(self, MsgCtx(msg, emit))


def _file_id(msg: dict[str, Any],
             kinds: tuple[str, ...]) -> str | None:
    """The largest matching payload's file id, if any."""
    if 'photo' in kinds and msg.get('photo'):
        best = max(msg['photo'], key=lambda p: p.get('file_size', 0))
        return str(best['file_id'])
    for kind in ('video', 'document'):
        if kind in kinds and msg.get(kind):
            return str(msg[kind]['file_id'])
    return None


class TgLinks(_TgSource):
    """Emit the first link of each message as a spooled .url file."""

    def accept(self, msg: dict[str, Any], emit: Emit) -> None:
        """Spool the link and emit its job."""
        _accept_link(self, MsgCtx(msg, emit))


class TgAny(_TgSource):
    """Emit links and media payloads from one long-poll dock.

    One token allows one getUpdates consumer, so a bot wanting both
    kinds runs one dock, never a merge of two Telegram docks.
    """

    def accept(self, msg: dict[str, Any], emit: Emit) -> None:
        """Prefer the link; fall through to the payload."""
        ctx = MsgCtx(msg, emit)
        if not _accept_link(self, ctx):
            _accept_media(self, ctx)
