# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Structural guards for the mixin-assembled Userbot (analysis, no import).

The Userbot is one class assembled from several ``*Mixin`` classes sharing
``self``. Two mistakes that class shape makes easy -- and that mypy/ruff do
NOT catch across separate files -- are guarded here by walking the AST:

* two mixins (or a mixin and Userbot) defining the SAME method name, so one
  silently shadows the other (this is exactly the _deliver bug that shipped);
* a mixin reading ``self.<name>`` that nothing on the assembled class provides.
"""

from __future__ import annotations

import ast
from pathlib import Path

_AGG = Path(__file__).resolve().parent.parent / 'minions' / 'userbot'
# base.py's UserbotProtocol only DECLARES the shared contract (stubs); it is
# not an implementer, so it is excluded from the duplicate-method check and
# read instead as the set of names the contract promises.
_PROTOCOL = 'UserbotProtocol'


def _impl_classes() -> dict[str, ast.ClassDef]:
    """Return Userbot + every ``*Mixin`` class def, keyed by class name."""
    found: dict[str, ast.ClassDef] = {}
    for path in _AGG.rglob('*.py'):
        tree = ast.parse(path.read_text(encoding='ascii'))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and (
                node.name == 'Userbot' or node.name.endswith('Mixin')
            ):
                found[node.name] = node
    return found


def _methods(cls: ast.ClassDef) -> set[str]:
    """Return the method names defined directly on a class body."""
    return {
        m.name
        for m in cls.body
        if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _self_reads(cls: ast.ClassDef) -> set[str]:
    """Return every ``self.<name>`` attribute read anywhere in the class."""
    reads: set[str] = set()
    for node in ast.walk(cls):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == 'self'
            and isinstance(node.ctx, ast.Load)
        ):
            reads.add(node.attr)
    return reads


def _self_assigns(cls: ast.ClassDef) -> set[str]:
    """Return every ``self.<name>`` attribute assigned in the class body."""
    assigns: set[str] = set()
    for node in ast.walk(cls):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == 'self'
            and isinstance(node.ctx, ast.Store)
        ):
            assigns.add(node.attr)
    return assigns


def _decl_name(stmt: ast.stmt) -> str | None:
    """Return the name a protocol body statement declares, or None."""
    if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
        return stmt.name
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return stmt.target.id
    return None


def _protocol_names() -> set[str]:
    """Return the attribute + method names UserbotProtocol declares."""
    base = next(p for p in _AGG.rglob('*.py') if p.name == 'base.py')
    tree = ast.parse(base.read_text(encoding='ascii'))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == _PROTOCOL:
            return {n for s in node.body if (n := _decl_name(s)) is not None}
    return set()


def test_no_duplicate_method_names_across_mixins() -> None:
    """No method name is defined on two of the assembled classes.

    A duplicate means one definition silently shadows the other once they are
    mixed into Userbot (the _deliver collision). Real overrides of a peer
    are not used here, so any collision is a bug.
    """
    owners: dict[str, list[str]] = {}
    for name, cls in _impl_classes().items():
        for method in _methods(cls):
            owners.setdefault(method, []).append(name)
    clashes = {m: cs for m, cs in owners.items() if len(cs) > 1}
    assert clashes == {}, f'method(s) defined on multiple classes: {clashes}'


def test_every_self_read_is_provided() -> None:
    """Every ``self.<name>`` a mixin reads is provided somewhere.

    Provided = a method on any assembled class, an attribute assigned on any
    of them (``self.x = ...``), or a name declared in UserbotProtocol.
    Catches a mixin that references state nothing supplies.
    """
    classes = _impl_classes()
    provided = _protocol_names()
    for cls in classes.values():
        provided |= _methods(cls)
        provided |= _self_assigns(cls)
    dunder_ok = {'__class__', '__dict__'}
    missing: dict[str, set[str]] = {}
    for name, cls in classes.items():
        gaps = _self_reads(cls) - provided - dunder_ok
        if gaps:
            missing[name] = gaps
    assert missing == {}, f'self references nothing provides: {missing}'
