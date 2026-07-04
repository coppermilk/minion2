"""Censor family tests: three bots, one shared step, documents only.

Covers the split (BLUEPRINT 9 waiver): HidePeople per mode, the
restore two-step belt, per-bot offsets, tokenless degradation
(REQ-DEG-001) and the documents-only Telegram contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

import pytest

import minions.censor_black.main
import minions.censor_blur.main
import minions.restore.main
from minion_core.adapters import llm
from minion_core.adapters import tg
from minion_core.adapters import vision
from minion_core.kernel import Disposition
from minion_core.kernel import Job
from minion_core.kernel import Origin
from tests.conftest import make_cfg
from tests.conftest import make_env

if TYPE_CHECKING:
    from pathlib import Path

    from minion_core.settings import Settings

BOTS = (
    minions.censor_blur.main,
    minions.censor_black.main,
    minions.restore.main,
)


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


def _full_mask(width: int, height: int) -> vision.Mask:
    """A person mask covering the whole frame (all pixels hidden)."""
    return vision.Mask(width, height, bytes([255]) * (width * height))


def test_hide_faces_delivers_s1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """censor-black: black out the face, deliver the ``_s1`` copy."""
    cfg = make_cfg(tmp_path / 'drive')
    src = _jpeg(cfg.bot_dir('censor-black') / 'pic.jpg')
    monkeypatch.setattr(vision, 'face_boxes', lambda p: ((8, 8, 24, 24),))
    verdict = vision.HideFaces().process(_job(cfg, 'censor-black', src))
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
    monkeypatch.setattr(vision, 'person_masks', lambda p: _full_mask(32, 32))
    verdict = vision.BlurContour().process(_job(cfg, 'censor-blur', src))
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
    monkeypatch.setattr(vision, 'face_boxes', lambda p: ())
    verdict = vision.HideFaces().process(_job(cfg, 'censor-black', src))
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
    monkeypatch.setattr(vision, 'person_masks', lambda p: None)
    verdict = vision.BlurContour().process(_job(cfg, 'censor-blur', src))
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
    monkeypatch.setattr(vision, 'person_boxes', lambda p: ((8, 8, 24, 24),))

    def fake_repaint(path: Path, spec: llm.LlmSpec) -> Path:
        out = path.with_stem(path.stem.removesuffix('_s1') + '_s2')
        out.write_bytes(b'repainted')
        return out

    monkeypatch.setattr(llm, 'restore_background', fake_repaint)
    spec = llm.spec_from({})
    hide = vision.HidePersonBoxes()
    first = hide.process(_job(cfg, 'restore', src))
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


def test_all_three_tokenless_mains_run(tmp_path: Path) -> None:
    """REQ-DEG-001 for the whole family."""
    make_cfg(tmp_path / 'drive')
    env = make_env(tmp_path / 'drive')
    for bot in BOTS:
        assert bot.main(env) == 0


def test_each_bot_owns_its_offset(tmp_path: Path) -> None:
    """Three identities, three high-water marks (STATE)."""
    cfg = make_cfg(tmp_path / 'drive')
    graphs = [bot.build(cfg, {'TG_TOKEN': ''}) for bot in BOTS]
    assert all(g is not None for g in graphs)
    names = {bot.BOT for bot in BOTS}
    assert names == {'censor-blur', 'censor-black', 'restore'}


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
