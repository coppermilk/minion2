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
        'minion_core/adapters/ollama.py',
        'minion_core/adapters/service_call.py',
    ),
    'yt_dlp': ('minion_core/adapters/fetch.py',),
    'PIL': ('minion_core/adapters/files.py',),
    'piexif': ('minion_core/adapters/files.py',),
    'numpy': (
        'minion_core/adapters/vision.py',
        'minions/svc/censor_blur/step.py',
    ),
    'torch': (
        'minion_core/adapters/vision.py',
        'minions/svc/censor_blur/step.py',
        'minions/svc/restore/step.py',
    ),
    'torchvision': (
        'minions/svc/censor_blur/step.py',
        'minions/svc/restore/step.py',
    ),
    'transformers': ('minion_core/adapters/vision.py',),
    'facenet_pytorch': ('minions/svc/censor_black/step.py',),
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


def _identity(parts: tuple[str, ...]) -> tuple[str, ...] | None:
    """The minion a path or import belongs to, or None if it is not one.

    minions/ groups by container logic: svc/<name> and bots/<name> are
    each a distinct minion (both segments identify it, so svc-a importing
    svc-b is a sibling breach), telegram is one package, and top-level
    modules (the catalog aggregator, the package __init__) belong to no
    single minion -- the aggregator names every Step by design.
    """
    if not parts or parts[0] != 'minions':
        return None
    rest = parts[1:]
    if rest[:1] == ('svc',) or rest[:1] == ('bots',):
        return tuple(rest[:2]) if len(rest) >= 2 else None
    if rest[:1] == ('telegram',):
        return ('telegram',)
    return None


def test_no_bot_imports_a_sibling_bot() -> None:
    """REQ-ARC-001: one minion never imports a sibling minion."""
    for path in _sources():
        rel = path.relative_to(REPO)
        me = _identity(rel.parts)
        if me is None:
            continue
        for name in _imports(path):
            them = _identity(tuple(name.split('.')))
            assert them is None or them == me, (
                f'{rel}: minion {me} imports sibling {them}'
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
