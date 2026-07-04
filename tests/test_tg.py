"""tg adapter tests: REQ-DATA-003, REQ-DEG-001, allow-list."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING
from typing import Any

from minion_core.adapters.files import free_quota
from minion_core.adapters.tg import OffsetStore
from minion_core.adapters.tg import SpoolSpec
from minion_core.adapters.tg import TgApi
from minion_core.adapters.tg import TgChannel
from minion_core.adapters.tg import TgLinks
from minion_core.adapters.tg import TgMedia
from minion_core.adapters.tg import TgSpec
from minion_core.adapters.tg import _document
from minion_core.adapters.tg import chats_from
from minion_core.adapters.tg import spool_of
from minion_core.kernel import Origin
from tests.conftest import make_cfg

if TYPE_CHECKING:
    from pathlib import Path

    from minion_core.settings import Settings


def test_offset_round_trip(tmp_path: Path) -> None:
    """REQ-DATA-003: the offset persists across restart."""
    store = OffsetStore(tmp_path / 'state' / 'bot.offset')
    assert store.read() == 0
    store.write(4242)
    reborn = OffsetStore(tmp_path / 'state' / 'bot.offset')
    assert reborn.read() == 4242


def test_corrupt_offset_is_loud_then_replays(tmp_path: Path) -> None:
    """offset_lost: corrupt state reads as 0 (replay), never crash."""
    path = tmp_path / 'bot.offset'
    path.write_text('garbage', encoding='ascii')
    assert OffsetStore(path).read() == 0


def _spec(cfg: Settings, chats: tuple[str, ...]) -> TgSpec:
    return TgSpec(
        spool=SpoolSpec(
            into=cfg.bot_dir('t'), budget=functools.partial(free_quota, cfg)
        ),
        dest=cfg.inbox,
        offset=cfg.state / 't.offset',
        chats=chats,
    )


def test_tokenless_source_ends_immediately(tmp_path: Path) -> None:
    """REQ-DEG-001: no token -> the dock ends; the belt lives on."""
    cfg = make_cfg(tmp_path / 'drive')
    source = TgLinks(TgApi(''), _spec(cfg, ('1',)))
    assert list(source(iter(()))) == []


def test_tokenless_channel_is_a_noop(tmp_path: Path) -> None:
    """REQ-DEG-001: replies vanish silently without a token."""
    channel = TgChannel(TgApi(''))
    origin = Origin('tg', '1:2:/spool/x')
    channel.send_text(origin, 'hello')  # must not raise, must not call
    channel.send_file(origin, tmp_path / 'x.bin')


class _ScriptedApi(TgApi):
    """API double: one batch of updates, then stop the source."""

    def __init__(self, updates: list[dict[str, Any]]) -> None:
        super().__init__(token='test-token')  # noqa: S106 -- double, not a secret
        self._updates = updates
        self.offsets_asked: list[int] = []
        self.owner: TgLinks | None = None

    def call(self, method: str, params: dict[str, Any]) -> Any:
        assert method == 'getUpdates'
        self.offsets_asked.append(params['offset'])
        if self.owner is not None:
            self.owner.stop()
        batch, self._updates = self._updates, []
        return batch


def _msg(chat: int, text: str) -> dict[str, Any]:
    return {
        'update_id': 7,
        'message': {'chat': {'id': chat}, 'message_id': 9, 'text': text},
    }


def test_links_source_spools_and_advances_offset(
    tmp_path: Path,
) -> None:
    """A link message becomes a .url spool; the offset advances."""
    cfg = make_cfg(tmp_path / 'drive')
    api = _ScriptedApi([_msg(1, 'grab https://example.com/v.mp4')])
    source = TgLinks(api, _spec(cfg, ('1',)))
    api.owner = source
    out = list(source(iter(())))
    assert len(out) == 1
    job = out[0].job
    assert job.src.suffix == '.url'
    assert job.src.read_text(encoding='ascii').startswith('https://')
    assert OffsetStore(cfg.state / 't.offset').read() == 8
    assert spool_of(job.origin) == job.src


def test_disallowed_chat_is_dropped(tmp_path: Path) -> None:
    """The allow-list is the primary control (OPERATIONS 3)."""
    cfg = make_cfg(tmp_path / 'drive')
    api = _ScriptedApi([_msg(666, 'https://example.com/x')])
    source = TgLinks(api, _spec(cfg, ('1',)))
    api.owner = source
    assert list(source(iter(()))) == []


class _RecordApi(TgApi):
    """API double that records downloads and sendMessage help replies."""

    def __init__(self) -> None:
        super().__init__(token='t')  # noqa: S106 -- double, not a secret
        self.name_seen: str | None = None
        self.messages: list[str] = []

    def call(self, method: str, params: dict[str, Any]) -> Any:
        assert method == 'sendMessage'
        self.messages.append(params['text'])
        return {}

    def download(
        self, file_id: str, spool: SpoolSpec, name: str | None = None
    ) -> Path:
        self.name_seen = name
        from minion_core.adapters.files import sanitize

        got = spool.into / sanitize(name or f'{file_id}.bin')
        got.parent.mkdir(parents=True, exist_ok=True)
        got.write_bytes(b'x')
        return got


def test_document_original_name_is_preserved(tmp_path: Path) -> None:
    """The sender's document.file_name reaches the spool, not file_0."""
    cfg = make_cfg(tmp_path / 'drive')
    api = _RecordApi()
    source = TgMedia(api, _spec(cfg, ('1',)))
    klip = ''.join(map(chr, (0x41A, 0x43B, 0x438, 0x43F)))  # Cyrillic
    msg = {
        'chat': {'id': 1},
        'message_id': 9,
        'document': {'file_id': 'FID', 'file_name': f'{klip} 5.mp4'},
    }
    emitted: list[Any] = []
    source.accept(msg, emitted.append)
    assert api.name_seen == f'{klip} 5.mp4'  # original passed through
    assert len(emitted) == 1
    assert emitted[0].job.src.name == f'{klip} 5.mp4'


def test_document_helper_refuses_compressed() -> None:
    """A photo/video payload is not a document: None (logged elsewhere)."""
    assert _document({'chat': {'id': 1}, 'photo': [{'file_id': 'x'}]}) is None
    doc = {'file_id': 'F', 'file_name': 'a.mp4'}
    assert _document({'chat': {'id': 1}, 'document': doc}) == doc


def test_help_reply_on_a_non_document(tmp_path: Path) -> None:
    """A message with no document/link gets a one-line usage + reminder."""
    cfg = make_cfg(tmp_path / 'drive')
    api = _RecordApi()
    spec = TgSpec(
        spool=SpoolSpec(
            into=cfg.bot_dir('t'), budget=functools.partial(free_quota, cfg)
        ),
        dest=cfg.inbox,
        offset=cfg.state / 't.offset',
        chats=('1',),
        help='I blur people.',
    )
    source = TgMedia(api, spec)
    source.accept(
        {'chat': {'id': 1}, 'message_id': 9, 'text': 'hi'}, lambda _e: None
    )
    assert len(api.messages) == 1
    assert 'I blur people.' in api.messages[0]
    assert 'document' in api.messages[0]  # the documents-only reminder


def test_malformed_update_never_wedges_the_dock(
    tmp_path: Path,
) -> None:
    """Untrusted payloads are validated explicitly (BLUEPRINT 4).

    A poison update is a logged bad_update; the dock survives, the
    offset advances past it, and the next good update is served.
    """
    cfg = make_cfg(tmp_path / 'drive')
    poison = {'update_id': 7, 'message': {'chat': {}}}  # no chat id
    good = {
        'update_id': 8,
        'message': {
            'chat': {'id': 1},
            'message_id': 9,
            'text': 'https://example.com/ok.mp4',
        },
    }
    api = _ScriptedApi([poison, good])
    source = TgLinks(api, _spec(cfg, ('1',)))
    api.owner = source
    out = list(source(iter(())))
    assert len(out) == 1  # the good update was served
    assert out[0].job.origin.ref.startswith('1:9:')
    assert OffsetStore(cfg.state / 't.offset').read() == 9  # past poison


def test_chats_from_parses_csv() -> None:
    """TG_CHATS is a comma-separated allow-list."""
    assert chats_from({'TG_CHATS': ' 1, 22 ,'}) == ('1', '22')
    assert chats_from({}) == ()


def test_two_whitelisted_users_served_without_crosstalk(
    tmp_path: Path,
) -> None:
    """One shared allow-list serves every listed chat concurrently.

    Each job's origin ref carries its own chat id, so replies and
    results always route back to the chat the message came from.
    """
    cfg = make_cfg(tmp_path / 'drive')
    api = _ScriptedApi(
        [
            _msg(1, 'https://example.com/a.mp4'),
            _msg(22, 'https://example.com/b.mp4'),
        ]
    )
    source = TgLinks(api, _spec(cfg, ('1', '22')))
    api.owner = source
    out = list(source(iter(())))
    assert len(out) == 2
    chats = [env.job.origin.ref.split(':', 1)[0] for env in out]
    assert chats == ['1', '22']  # each reply routes to its own chat


def test_spool_of_survives_windows_paths() -> None:
    """The third ref field may itself contain colons."""
    origin = Origin('tg', r'5:6:C:\Users\a\My Drive\bots\x.jpg')
    got = spool_of(origin)
    assert got is not None
    assert str(got).endswith('x.jpg')
    assert spool_of(Origin('tg', '5:6')) is None
