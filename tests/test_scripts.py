"""scripts adapter tests: .gdoc consumption, archive, degradation."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from typing import ClassVar

from minion_core.adapters import scripts
from tests.conftest import make_cfg

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _gdoc(path: Path, doc_id: str = 'abc123') -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'doc_id': doc_id}), encoding='ascii')
    return path


def _fake_fetch(
    monkeypatch: pytest.MonkeyPatch, texts: dict[str, str]
) -> list[str]:
    """Route read_script_doc by document id; record the calls."""
    calls: list[str] = []

    def fake(doc_id: str) -> str:
        calls.append(doc_id)
        return texts.get(doc_id, '')

    monkeypatch.setattr(scripts, 'read_script_doc', fake)
    return calls


def test_doc_id_extracted_from_full_url() -> None:
    """Doc ids arrive bare or as full Docs URLs."""
    url = 'https://docs.google.com/document/d/aB1-_c/edit?tab=t.0'
    match = scripts._DOC_ID.search(url)
    assert match is not None
    assert match.group(1) == 'aB1-_c'
    assert scripts.read_script_doc('') == ''  # empty in, empty out


def test_bad_doc_id_never_reaches_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crafted id with URL metacharacters is rejected, not fetched.

    A ``.gdoc`` is untrusted input; only an opaque base64url id may be
    interpolated into the export URL, or a stray ``/`` / ``?`` could
    retarget the credential-less request.
    """
    called = False

    def guard(*_a: object, **_k: object) -> object:
        nonlocal called
        called = True
        raise AssertionError

    monkeypatch.setattr('requests.get', guard, raising=False)
    assert scripts.read_script_doc('../../secret') == ''
    assert scripts.read_script_doc('id?x=y') == ''
    assert scripts.read_script_doc('a/b') == ''
    assert not called


def test_gdoc_shortcuts_consumed_and_combined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Several .gdoc at once: texts joined, shortcuts deleted."""
    cfg = make_cfg(tmp_path / 'drive')
    _gdoc(cfg.inbox / 'a_script.gdoc', 'one')
    _gdoc(cfg.inbox / 'b_script.gdoc', 'two')
    calls = _fake_fetch(
        monkeypatch, {'one': 'SCENE CAKE', 'two': 'SCENE OFFICE'}
    )
    text = scripts.read_scripts_from_inbox(cfg.inbox)
    assert text == 'SCENE CAKE\n\nSCENE OFFICE'
    assert calls == ['one', 'two']
    assert list(cfg.inbox.glob('*.gdoc')) == []


def test_gdoc_id_from_url_field(tmp_path: Path) -> None:
    """Older Drive clients store the id in the url field only."""
    shortcut = tmp_path / 'old.gdoc'
    shortcut.write_text(
        json.dumps({'url': 'https://docs.google.com/document/d/xyz9/edit'}),
        encoding='ascii',
    )
    assert scripts._id_from_gdoc(shortcut) == 'xyz9'


def test_dead_shortcut_is_still_consumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shortcut without an id must not wedge every later run."""
    cfg = make_cfg(tmp_path / 'drive')
    bad = cfg.inbox / 'broken.gdoc'
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text('not json', encoding='ascii')
    _fake_fetch(monkeypatch, {})
    assert scripts.read_scripts_from_inbox(cfg.inbox) == ''
    assert not bad.exists()


def test_hint_archived_and_served_after_consumption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The week's text outlives its one-shot .gdoc shortcut."""
    cfg = make_cfg(tmp_path / 'drive')
    _gdoc(cfg.inbox / 'week.gdoc', 'one')
    _fake_fetch(monkeypatch, {'one': 'SCENE CAKE DINNER'})
    assert scripts.script_hint(cfg) == 'SCENE CAKE DINNER'
    assert list(cfg.inbox.glob('*.gdoc')) == []
    assert list(cfg.scripts.glob('*.txt'))  # archived
    # The next run (any machine) reads the archive, no shortcuts left.
    assert scripts.script_hint(cfg) == 'SCENE CAKE DINNER'


def test_no_shortcut_no_archive_is_empty(tmp_path: Path) -> None:
    """No source at all degrades to an empty hint."""
    cfg = make_cfg(tmp_path / 'drive')
    assert scripts.script_hint(cfg) == ''


def test_fetch_failure_degrades_to_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing fetch consumes the shortcut and yields ''."""
    cfg = make_cfg(tmp_path / 'drive')
    _gdoc(cfg.inbox / 'week.gdoc', 'one')
    _fake_fetch(monkeypatch, {})  # every fetch returns ''
    assert scripts.script_hint(cfg) == ''
    assert list(cfg.inbox.glob('*.gdoc')) == []


def test_export_response_must_be_plain_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A login page (non text/plain) reads as a private doc: ''."""

    class Resp:
        headers: ClassVar[dict[str, str]] = {'Content-Type': 'text/html'}
        text = '<html>login</html>'

        def raise_for_status(self) -> None:
            return

    monkeypatch.setattr(
        'requests.get',
        lambda url, timeout, allow_redirects: Resp(),
        raising=False,
    )
    assert scripts.read_script_doc('abc') == ''


def test_long_script_is_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The injected hint is bounded (BLUEPRINT 4)."""

    class Resp:
        headers: ClassVar[dict[str, str]] = {'Content-Type': 'text/plain'}
        text = 'x' * (scripts.MAX_SCRIPT_CHARS + 100)

        def raise_for_status(self) -> None:
            return

    monkeypatch.setattr(
        'requests.get',
        lambda url, timeout, allow_redirects: Resp(),
        raising=False,
    )
    assert len(scripts.read_script_doc('abc')) == scripts.MAX_SCRIPT_CHARS
