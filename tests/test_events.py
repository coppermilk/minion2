"""Phase 1.5: event taps fire as items flow through graph nodes.

When a BuildContext carries an emitter, the loader wraps every node in a
tap: each item emits 'entered' then 'left' (with the node's disposition)
as it crosses. The default (no emitter) path stays plain -- covered by
test_graph. Taps must survive the merge's threads (Collector is
thread-safe) and must not change what the belt decides.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from minion_core.adapters import video
from minion_core.events import Collector
from minion_core.events import Event
from minion_core.graph import context
from minion_core.graph import load
from minion_core.kernel import Disposition
from minions.graph import read
from minions.service import CATALOG
from tests.conftest import make_cfg

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


def _observed(spec, cfg, collector) -> list[Event]:
    env = {'DRIVE': str(cfg.drive)}
    ctx = replace(context(cfg, env, spec['bot']), emit=collector)
    return list(load(spec, ctx, CATALOG)(iter(())))


def _batch(spec) -> None:
    for stage in spec['stages']:
        for node in stage.get('merge', [stage]):
            if node.get('source') == 'folder':
                node['once'] = True


def _fake_frames(src: Path, out: Path, spec: video.FrameSpec) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    shots = [out / f'frame_{i:04d}.jpg' for i in range(1, 4)]
    for shot in shots:
        shot.write_bytes(b'jpg')
    return shots


def test_taps_emit_entered_then_left(tmp_path) -> None:
    """A dropped file crosses the folder, then enters and leaves deliver."""
    cfg = make_cfg(tmp_path / 'drive')
    drop = cfg.bot_dir('demo') / 'a.jpg'
    drop.parent.mkdir(parents=True, exist_ok=True)
    drop.write_bytes(b'img')

    col = Collector()
    _observed(DELIVER_SPEC, cfg, col)
    events = col.events

    def phases(node: str) -> list[str]:
        return [e.phase for e in events if e.node == node]

    # ids are assigned by position: folder is #0, deliver is #1.
    assert phases('source:folder#0') == ['left']
    assert phases('step:deliver#1') == ['entered', 'left']
    left = next(
        e for e in events if e.node == 'step:deliver#1' and e.phase == 'left'
    )
    assert left.disposition == Disposition.DELIVERED.value
    # Order: the file leaves the folder, enters deliver, then leaves it.
    order = [(e.node, e.phase) for e in events]
    assert order.index(('source:folder#0', 'left')) < order.index(
        ('step:deliver#1', 'entered')
    )


def test_taps_survive_the_merge_threads(tmp_path, monkeypatch) -> None:
    """The frames merge (tg | folder) taps the threaded dock too."""
    monkeypatch.setattr(video, 'frames', _fake_frames)
    monkeypatch.setattr(video, 'probe_fps', lambda p, t: 5.0)

    cfg = make_cfg(tmp_path / 'drive')
    spec = read(str(REPO / 'minions' / 'frames' / 'graph.json'))
    _batch(spec)
    clip = cfg.bot_dir('frames') / 'clip.mp4'
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b'video')

    col = Collector()
    _observed(spec, cfg, col)
    events = col.events

    # The frames step delivered, and a tap saw it leave delivered.
    assert any(
        e.phase == 'left'
        and e.node.startswith('step:frames')
        and e.disposition == Disposition.DELIVERED.value
        for e in events
    )
    # The folder dock, running on the merge's thread, still emitted.
    assert any(e.node.startswith('source:folder') for e in events)
