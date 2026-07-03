"""Structural analysis: REQ-ARC-001/002 and the import direction.

DO-178C menu: these requirements are discharged by analysis, not by
runtime tests -- the suite walks the AST of every source file.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

VENDORS = {
    'requests': (
        'minion_core/adapters/tg.py',
        'minion_core/adapters/scripts.py',
    ),
    'yt_dlp': ('minion_core/adapters/fetch.py',),
    'PIL': ('minion_core/adapters/files.py',),
    'piexif': ('minion_core/adapters/files.py',),
    'numpy': ('minion_core/adapters/vision.py',),
    'torch': ('minion_core/adapters/vision.py',),
    'torchvision': ('minion_core/adapters/vision.py',),
    'transformers': ('minion_core/adapters/vision.py',),
    'facenet_pytorch': ('minion_core/adapters/vision.py',),
    'google': ('minion_core/adapters/llm.py',),
}
"""Each vendor and its sanctioned import sites (adapters only)."""


def _sources() -> list[Path]:
    files = [
        *(REPO / 'minion_core').rglob('*.py'),
        *(REPO / 'minions').rglob('*.py'),
    ]
    assert files, 'source tree not found'
    return files


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding='ascii'))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_no_bot_imports_a_sibling_bot() -> None:
    """REQ-ARC-001: minions/<a> never imports minions/<b>."""
    for path in _sources():
        rel = path.relative_to(REPO)
        if rel.parts[0] != 'minions' or len(rel.parts) < 3:
            continue
        me = rel.parts[1]
        for name in _imports(path):
            parts = name.split('.')
            if parts[0] != 'minions' or len(parts) < 2:
                continue
            assert parts[1] == me, (
                f'{rel}: bot {me!r} imports sibling {parts[1]!r}'
            )


def test_vendors_only_behind_their_adapter() -> None:
    """REQ-ARC-002: vendors import only at their sanctioned sites."""
    for path in _sources():
        rel = str(path.relative_to(REPO)).replace('\\', '/')
        for name in _imports(path):
            owners = VENDORS.get(name.split('.')[0])
            if owners is None:
                continue
            assert rel in owners, f'{rel}: vendor {name!r} belongs to {owners}'


def test_kernel_imports_stdlib_only() -> None:
    """Import direction: the kernel sits at the bottom."""
    stdlib = sys.stdlib_module_names
    for name in _imports(REPO / 'minion_core' / 'kernel.py'):
        root = name.split('.')[0]
        assert root in stdlib, f'kernel imports non-stdlib {name!r}'


def test_adapters_never_import_bots() -> None:
    """Import direction: adapters -> kernel/settings, never up."""
    for path in (REPO / 'minion_core').rglob('*.py'):
        for name in _imports(path):
            assert not name.startswith('minions'), (
                f'{path.name}: core imports a bot ({name})'
            )


def test_no_host_os_branching_outside_adapters() -> None:
    """REQ-PRT-001 analysis: no host-OS branch outside adapters/.

    BLUEPRINT 1.2: the software never branches on the host OS; the
    spooler difference is configuration, not a platform read.
    """
    for path in _sources():
        rel = str(path.relative_to(REPO)).replace('\\', '/')
        if rel.startswith('minion_core/adapters/'):
            continue
        source = path.read_text(encoding='ascii')
        assert 'sys.platform' not in source, (
            f'{rel}: host-OS branch outside adapters/'
        )
        assert 'platform.system' not in source, (
            f'{rel}: host-OS branch outside adapters/'
        )
