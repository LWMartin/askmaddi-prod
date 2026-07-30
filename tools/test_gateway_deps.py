"""GATEWAY_DEPS must match what gateway/ actually imports.

`gateway_capable()` decides whether an interpreter gates gateway/ or only
tools/. It answers by trying to import GATEWAY_DEPS — so if that tuple does
not describe the real import surface, the gate is wrong in one of two ways
and neither announces itself:

  - listing a module gateway/ does NOT hard-require marks a capable
    interpreter NARROW, quietly shrinking coverage. This is what happened:
    `flask_limiter` sat in the tuple from the start while being a guarded
    optional import (try/except ImportError -> HAS_LIMITER), absent from
    requirements.txt, referenced by no test. Both production interpreters
    were reported narrow on 2026-07-30 partly because of a module neither
    needed.
  - omitting a module gateway/ DOES hard-require lets gateway_capable answer
    True for an interpreter where collection then fails. `requests` was a
    hard module-level import in three gateway/ files and was not listed.

So this test does not restate the list — restating is how it drifted. It
re-derives the surface from the tree and compares.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bot_push  # noqa: E402

GATEWAY = Path(__file__).resolve().parent.parent / 'gateway'

# Imported by tests rather than by the code under test; every gate interpreter
# is already checked for pytest by usable_gate_pythons(), so it is not a
# GATEWAY_DEPS concern.
TEST_ONLY = {'pytest'}


def _guarded_names(tree: ast.AST) -> set[str]:
    """Top-level names imported inside a try — optional by construction."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for sub in ast.walk(node):
                if isinstance(sub, ast.ImportFrom) and sub.module:
                    out.add(sub.module.split('.')[0])
                elif isinstance(sub, ast.Import):
                    for a in sub.names:
                        out.add(a.name.split('.')[0])
    return out


def _local_modules() -> set[str]:
    """Repo-local module names — importable by bare name, not installable."""
    root = GATEWAY.parent
    names = {p.stem for p in GATEWAY.glob('*.py')}
    names |= {p.stem for p in (root / 'tools').glob('*.py')}
    names |= {d.name for d in root.iterdir()
              if d.is_dir() and (d / '__init__.py').exists()}
    names.add('gateway')
    return names


def _module_level_imports(path: Path) -> set[str]:
    """Unguarded, module-level top names imported by one file."""
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    guarded = _guarded_names(tree)
    out = set()
    for node in ast.walk(tree):          # ANY depth — see the docstring
        names = []
        if isinstance(node, ast.Import):
            names = [a.name.split('.')[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:                          # relative, local
                continue
            if node.module:
                names = [node.module.split('.')[0]]
        out.update(n for n in names if n not in guarded)
    return out


def hard_third_party_imports() -> set[str]:
    """Third-party modules pytest must import to COLLECT gateway/.

    Walks the import graph from the test files, not every file in the tree.
    The distinction is load-bearing: gateway/headless_fetcher.py imports
    selenium and undetected_chromedriver at module level and unguarded, but no
    test imports it and app_production pulls it inside a try — so collection
    never touches selenium, and requiring it would mark every production
    interpreter narrow for a module the gate does not need.
    """
    local = _local_modules()
    stdlib = getattr(sys, 'stdlib_module_names', set())
    seen, found = set(), set()
    work = [p for p in sorted(GATEWAY.glob('test_*.py'))]
    conftest = GATEWAY / 'conftest.py'
    if conftest.exists():
        work.append(conftest)

    while work:
        path = work.pop()
        if path in seen:
            continue
        seen.add(path)
        for name in _module_level_imports(path):
            if name in stdlib or name in TEST_ONLY:
                continue
            if name in local:
                sibling = GATEWAY / f'{name}.py'
                if sibling.exists():
                    work.append(sibling)          # follow into gateway/
                continue                           # local: never a dep
            found.add(name)
    return found


def test_gateway_deps_match_imports():
    """The whole point: the tuple is checked against the tree, not restated."""
    assert set(bot_push.GATEWAY_DEPS) == hard_third_party_imports()


def test_optional_imports_are_not_required():
    """A guarded import must never enter GATEWAY_DEPS — that is the mistake
    this file exists to prevent, and flask_limiter is the live example."""
    assert 'flask_limiter' not in bot_push.GATEWAY_DEPS
    src = (GATEWAY / 'app_production.py').read_text(encoding='utf-8')
    assert 'except ImportError' in src and 'flask_limiter' in src, (
        'flask_limiter is expected to remain a GUARDED optional import; if it '
        'became mandatory, GATEWAY_DEPS must gain it and this test must change')


def test_the_derivation_actually_finds_something():
    """Guard the guard: a walker that silently found nothing would make
    test_gateway_deps_match_imports pass against an empty tuple."""
    found = hard_third_party_imports()
    assert found, 'derivation found no third-party imports — walker is broken'
    assert 'flask' in found


def test_local_gateway_modules_are_not_treated_as_dependencies():
    """gateway/ imports its own siblings bare (env_bootstrap, work_queue).
    Those are not installable deps and must never reach GATEWAY_DEPS."""
    assert 'env_bootstrap' not in bot_push.GATEWAY_DEPS
    assert 'work_queue' not in bot_push.GATEWAY_DEPS
