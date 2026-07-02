"""The ASCII law, self-applied repo-wide (BLUEPRINT 4)."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

CHECKED_SUFFIXES = (
    '.py',
    '.md',
    '.toml',
    '.yml',
    '.yaml',
    '.cfg',
    '.ini',
    '.txt',
    '.example',
    '.gs',
)

SKIP_DIRS = {
    '.git',
    '.venv',
    'venv',
    '__pycache__',
    '.mypy_cache',
    '.pytest_cache',
    '.ruff_cache',
    'dist',
    'build',
    '.eggs',
}


def _tracked() -> list[Path]:
    found = []
    for path in REPO.rglob('*'):
        if not path.is_file():
            continue
        if SKIP_DIRS & set(path.relative_to(REPO).parts):
            continue
        if path.suffix.lower() in CHECKED_SUFFIXES:
            found.append(path)
    assert found, 'nothing to check'
    return found


def test_repo_is_ascii_only() -> None:
    """Every text file decodes as pure ASCII."""
    dirty = []
    for path in _tracked():
        try:
            path.read_bytes().decode('ascii')
        except UnicodeDecodeError:
            dirty.append(str(path.relative_to(REPO)))
    assert dirty == [], f'non-ASCII files: {dirty}'
