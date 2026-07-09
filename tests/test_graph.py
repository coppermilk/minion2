"""Phase 1: a graph built from data equals the hand-written build().

The loader must assemble the same belt the Python build() does: a
folder->deliver spec behaves identically to the hand-built stages
(golden), the shipped inbox spec degrades cleanly tokenless like its
build() (REQ-DEG-001), and the shipped frames spec reproduces the
frames extraction. Bad specs fail loud (BadGraph).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from minion_core.adapters import video
from minion_core.adapters.files import Deliver
from minion_core.graph import BadGraph
from minion_core.kernel import Disposition
from minion_core.kernel import Folder
from minion_core.kernel import FolderSpec
from minion_core.kernel import SeenPaths
from minions.graph import build
from minions.graph import read
from tests.conftest import make_cfg

if TYPE_CHECKING:
    from minion_core.kernel import Stage
    from minion_core.kernel import Verdict

REPO = Path(__file__).resolve().parent.parent

DELIVER_SPEC = {
    'bot': 'demo',
    'stages': [
        {
            'source': 'folder',
            'root': 'bot_dir',
            'into': 'inbox',
            'exts': ['.jpg'],
            'once': True,
        },
        {'step': 'deliver'},
    ],
}


def _delivered(graph: Stage) -> Verdict:
    outs = [
        e.verdict
        for e in graph(iter(()))
        if e.verdict and e.verdict.disposition is Disposition.DELIVERED
    ]
    assert len(outs) == 1
    return outs[0]


def _drop(cfg, name: str, data: bytes = b'img') -> Path:
    path = cfg.bot_dir('demo') / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_folder_deliver_graph_matches_handbuilt(tmp_path) -> None:
    """Golden: the data graph delivers exactly like the code graph."""
    cfg1 = make_cfg(tmp_path / 'd1')
    _drop(cfg1, 'a.jpg')
    env1 = {'DRIVE': str(tmp_path / 'd1')}
    got1 = _delivered(build(DELIVER_SPEC, cfg1, env1))

    cfg2 = make_cfg(tmp_path / 'd2')
    _drop(cfg2, 'a.jpg')
    spec = FolderSpec(
        root=cfg2.bot_dir('demo'),
        dest=cfg2.inbox,
        exts=('.jpg',),
        poll_sec=cfg2.poll_sec,
        once=True,
    )
    hand = Folder(spec, SeenPaths(cfg2.seen_paths_max)) >> Deliver()
    got2 = _delivered(hand)

    assert got1.disposition is got2.disposition is Disposition.DELIVERED
    assert got1.result is not None
    assert got2.result is not None
    assert got1.result.name == got2.result.name  # same canonical stem
    assert (cfg1.inbox / got1.result.name).is_file()
    assert not (cfg1.bot_dir('demo') / 'a.jpg').exists()  # source consumed


def test_inbox_graph_drains_clean_tokenless(tmp_path) -> None:
    """REQ-DEG-001: the shipped inbox spec is a clean no-op tokenless."""
    cfg = make_cfg(tmp_path / 'drive')
    spec = read(str(REPO / 'minions' / 'inbox' / 'graph.json'))
    env = {'DRIVE': str(tmp_path / 'drive'), 'TG_TOKEN': '', 'TG_CHATS': '1'}
    assert list(build(spec, cfg, env)(iter(()))) == []


def _batch(spec) -> None:
    for stage in spec['stages']:
        for node in stage.get('merge', [stage]):
            if node.get('source') == 'folder':
                node['once'] = True


def test_frames_graph_extracts_like_build(tmp_path, monkeypatch) -> None:
    """The shipped frames spec reproduces the frames extraction."""

    def fake_frames(src: Path, out: Path, spec: video.FrameSpec) -> list[Path]:
        out.mkdir(parents=True, exist_ok=True)
        shots = [out / f'frame_{i:04d}.jpg' for i in range(1, 4)]
        for shot in shots:
            shot.write_bytes(b'jpg')
        return shots

    monkeypatch.setattr(video, 'frames', fake_frames)
    monkeypatch.setattr(video, 'probe_fps', lambda p, t: 5.0)

    cfg = make_cfg(tmp_path / 'drive')
    spec = read(str(REPO / 'minions' / 'frames' / 'graph.json'))
    _batch(spec)
    clip = cfg.bot_dir('frames') / 'clip.mp4'
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b'video')

    verdict = _delivered(build(spec, cfg, {'DRIVE': str(tmp_path / 'drive')}))
    assert verdict.result is not None
    assert verdict.result.parent == cfg.bot_done('frames')
    names = sorted(p.name for p in verdict.result.iterdir() if p.is_file())
    assert names == ['0-0_clip.jpg', '1-5_clip.jpg', '2-10_clip.jpg']
    assert 'frames' in verdict.reply


def test_unknown_dir_alias_is_loud(tmp_path) -> None:
    """A bad directory alias fails at build, not at run."""
    cfg = make_cfg(tmp_path / 'drive')
    spec = {
        'bot': 'x',
        'stages': [{'source': 'folder', 'root': 'nope', 'exts': ['.jpg']}],
    }
    with pytest.raises(BadGraph):
        build(spec, cfg, {'DRIVE': str(tmp_path / 'drive')})


def test_unknown_node_kind_is_loud(tmp_path) -> None:
    """A node with no known kind fails loud."""
    cfg = make_cfg(tmp_path / 'drive')
    spec = {'bot': 'x', 'stages': [{'mystery': '?'}]}
    with pytest.raises(BadGraph):
        build(spec, cfg, {'DRIVE': str(tmp_path / 'drive')})
