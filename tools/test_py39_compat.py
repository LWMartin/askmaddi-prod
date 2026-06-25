#!/usr/bin/env python3
"""
test_py39_compat.py — guard the deployment-version floor (Python 3.9, AlmaLinux 9).
==================================================================================
The sandbox/CI runs Python 3.11+/3.12; the VPS runs the AlmaLinux 9 system
python, 3.9. That gap is a SILENT blind spot: code using 3.10+ syntax passes
every test in the sandbox and then crashes on the box. It bit us 2026-06-25 —
two test files used PEP 604 union annotations (`Path | None`), which raise
TypeError at import on 3.9, crashing pytest COLLECTION (exit 2). That failed the
nightly writeback gate (bot_push runs `python -m pytest tools/`) for days,
unseen, because no sandbox python is old enough to reproduce it.

This guard closes the gap WITHOUT needing a 3.9 interpreter in CI: it parses
every repo .py with `ast` and flags the 3.9-illegal patterns. Because it lives in
tools/, it runs inside the bot gate — so a 3.9-incompatible annotation now fails
in the sandbox at test time, loudly, instead of on the box at deploy time,
silently.

Currently guarded:
  - PEP 604 unions (`X | Y`) in annotation position without
    `from __future__ import annotations` (which defers them to strings, 3.9-safe).

The fix when this test fails is almost always: add `from __future__ import
annotations` to the offending file (one line, fixes the whole file), or use
`Optional[X]` / `Union[X, Y]`.
"""
import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKIP_PARTS = {"venv", "__pycache__", ".git", "node_modules"}


def _iter_py_files():
    for p in REPO_ROOT.rglob("*.py"):
        if SKIP_PARTS & set(p.parts):
            continue
        yield p


def _has_future_annotations(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            if any(alias.name == "annotations" for alias in node.names):
                return True
    return False


def _pep604_union_lines(tree):
    """Return line numbers of `X | Y` BinOps used in annotation position."""
    lines = []

    def scan_annotation(node):
        for n in ast.walk(node):
            if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr):
                lines.append(getattr(node, "lineno", -1))

    class V(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            if node.returns:
                scan_annotation(node.returns)
            for arg in (node.args.args + node.args.posonlyargs
                        + node.args.kwonlyargs):
                if arg.annotation:
                    scan_annotation(arg.annotation)
            self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_AnnAssign(self, node):
            if node.annotation:
                scan_annotation(node.annotation)
            self.generic_visit(node)

    V().visit(tree)
    return sorted(set(lines))


def test_no_pep604_unions_without_future_import():
    """No repo .py may use `X | Y` annotations without future-annotations.

    Such files import-crash on Python 3.9 (the VPS floor), taking the whole
    pytest collection down with them — the failure mode that silently wedged the
    writeback gate. Catch it here, in the sandbox, at test time.
    """
    offenders = []
    for path in _iter_py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as e:
            offenders.append(f"{path.relative_to(REPO_ROOT)}: unparseable ({e})")
            continue
        if _has_future_annotations(tree):
            continue  # unions are deferred to strings → 3.9-safe
        hits = _pep604_union_lines(tree)
        if hits:
            offenders.append(
                f"{path.relative_to(REPO_ROOT)}: PEP 604 union (X | Y) at "
                f"line(s) {hits} without `from __future__ import annotations` "
                f"— crashes on Python 3.9 (VPS)."
            )

    assert not offenders, (
        "3.9-incompatible annotation syntax found (breaks the VPS gate):\n  "
        + "\n  ".join(offenders)
        + "\n\nFix: add `from __future__ import annotations` to each file, or use "
          "Optional[X] / Union[X, Y]."
    )
