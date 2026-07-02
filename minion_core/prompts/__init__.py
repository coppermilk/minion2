"""LLM prompt texts as package-data (BLUEPRINT 6, 12).

One place per fact: every prompt the system sends lives here as a
Markdown file, loaded by name.
"""

from __future__ import annotations

from importlib import resources


def load_prompt(name: str) -> str:
    """Return the prompt text stored as ``prompts/<name>.md``."""
    pkg = resources.files(__package__)
    return (pkg / f'{name}.md').read_text(encoding='ascii')
