"""Censor family tests: the three service Steps and the tg contract.

Covers HideFaces / BlurContour / HidePersonBoxes (each now its own service
minion), the restore two-step belt, and the documents-only Telegram contract
(REQ-DEG-001 folder degradation now lives with the relay, not these Steps).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

import pytest

from minion_core.adapters import llm
from minion_core.adapters import tg
from minion_core.adapters.files import Mask
from minion_core.kernel import Disposition
from minion_core.kernel import Job
from minion_core.kernel import Origin
from minions.svc.censor_black import step as black
from minions.svc.censor_blur import step as blur
from minions.svc.restore import step as boxes
from tests.conftest import make_cfg

if TYPE_CHECKING:
    from pathlib import Path

    from minion_core.settings import Settings


def _jpeg(path: Path) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (32, 32), (240, 240, 240)).save(path, 'JPEG')
    return path


def _job(cfg: Settings, bot: str, src: Path) -> Job:
    return Job(
        src=src,
        dest=cfg.bot_dir(bot),
        stem=src.stem,
        origin=Origin('tg', f'1:2:{src}'),
    )


def _full_mask(width: int, height: int) -> Mask:
    """A person mask covering the whole frame (all pixels hidden)."""
    return Mask(width, height, bytes([255]) * (width * height))


def test_hide_faces_delivers_s1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """censor-black: black out the face, deliver the ``_s1`` copy."""
    cfg = make_cfg(tmp_path / 'drive')
    src = _jpeg(cfg.bot_dir('censor-black') / 'pic.jpg')
    monkeypatch.setattr(black, 'face_boxes', lambda p: ((8, 8, 24, 24),))
    verdict = black.HideFaces().process(_job(cfg, 'censor-black', src))
    assert verdict.disposition is Disposition.DELIVERED
    assert verdict.result is not None
    assert verdict.result.name == 'pic_s1.jpg'
    assert verdict.result.is_file()
    assert src.is_file()  # the original is untouched by the step


def test_blur_contour_delivers_s1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """censor-blur: blur the person silhouette, deliver the ``_s1`` copy."""
    cfg = make_cfg(tmp_path / 'drive')
    src = _jpeg(cfg.bot_dir('censor-blur') / 'pic.jpg')
    monkeypatch.setattr(blur, 'person_masks', lambda p: _full_mask(32, 32))
    verdict = blur.BlurContour().process(_job(cfg, 'censor-blur', src))
    assert verdict.disposition is Disposition.DELIVERED
    assert verdict.result is not None
    assert verdict.result.name == 'pic_s1.jpg'
    assert verdict.result.is_file()
    assert src.is_file()


def test_no_face_is_skip_never_passthrough(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CT-B: zero detections must not send the original back."""
    cfg = make_cfg(tmp_path / 'drive')
    src = _jpeg(cfg.bot_dir('censor-black') / 'pic.jpg')
    monkeypatch.setattr(black, 'face_boxes', lambda p: ())
    verdict = black.HideFaces().process(_job(cfg, 'censor-black', src))
    assert verdict.disposition is Disposition.SKIPPED
    assert verdict.reason == 'no_face'
    assert verdict.result is None


def test_no_person_contour_is_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CT-B for the blur bot: no mask -> SKIP, not a pass-through."""
    cfg = make_cfg(tmp_path / 'drive')
    src = _jpeg(cfg.bot_dir('censor-blur') / 'pic.jpg')
    monkeypatch.setattr(blur, 'person_masks', lambda p: None)
    verdict = blur.BlurContour().process(_job(cfg, 'censor-blur', src))
    assert verdict.disposition is Disposition.SKIPPED
    assert verdict.reason == 'no_person'
    assert verdict.result is None


def test_restore_belt_delivers_s2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The restore belt has two steps: blur ``_s1``, repaint ``_s2``."""
    cfg = make_cfg(tmp_path / 'drive')
    src = _jpeg(cfg.bot_dir('restore') / 'pic.jpg')
    monkeypatch.setattr(boxes, 'person_boxes', lambda p: ((8, 8, 24, 24),))

    def fake_repaint(path: Path, spec: llm.LlmSpec) -> Path:
        out = path.with_stem(path.stem.removesuffix('_s1') + '_s2')
        out.write_bytes(b'repainted')
        return out

    monkeypatch.setattr(llm, 'restore_background', fake_repaint)
    spec = llm.spec_from({})
    first = boxes.HidePersonBoxes().process(_job(cfg, 'restore', src))
    assert first.result is not None
    assert first.result.name == 'pic_s1.jpg'
    second = llm.RestoreBackground(spec).process(
        _job(cfg, 'restore', first.result)
    )
    assert second.disposition is Disposition.DELIVERED
    assert second.result is not None
    assert second.result.name == 'pic_s2.jpg'
    assert first.result.is_file()  # _s1 stays in the work dir


def test_restore_refusal_is_stable_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An LlmError maps to restore_failed, never a crash."""
    cfg = make_cfg(tmp_path / 'drive')
    s1 = _jpeg(cfg.bot_dir('restore') / 'pic_s1.jpg')

    def refuse(path: Path, spec: llm.LlmSpec) -> Path:
        raise llm.LlmError('no image in restore response')

    monkeypatch.setattr(llm, 'restore_background', refuse)
    verdict = llm.RestoreBackground(llm.spec_from({})).process(
        _job(cfg, 'restore', s1)
    )
    assert verdict.disposition is Disposition.FAILED
    assert verdict.reason == 'restore_failed'
    assert s1.is_file()


def _msg(payload: dict[str, Any]) -> dict[str, Any]:
    return {'chat': {'id': 1}, 'message_id': 9, **payload}


def test_documents_only_refuses_compressed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A compressed photo is ignored and logged not_a_document."""
    photo = _msg({'photo': [{'file_id': 'p1', 'file_size': 10}]})
    with caplog.at_level('WARNING', logger='tg'):
        assert tg._document(photo) is None
    assert 'not_a_document' in caplog.text


def test_documents_only_accepts_documents() -> None:
    """A document payload is the one accepted kind."""
    doc = _msg({'document': {'file_id': 'd1'}})
    got = tg._document(doc)
    assert got is not None
    assert got['file_id'] == 'd1'
    assert tg._document(_msg({'text': 'hello'})) is None
