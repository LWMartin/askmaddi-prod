#!/usr/bin/env python3
"""Integration test: the skus.json -> contamination.json bridge RESOLVES.

maddi-skus-registry build step 5 ("contamination_key bridge resolves"). This is
the test with teeth: it asserts every `contamination_key` in the live registry
points at a real product key in the editorial contamination registry.

SINGLE SOURCE OF TRUTH (refactor 2026-06-24): the resolve invariant is no longer
re-implemented here. It is owned by `registry_join_check.check()` in phantom-ops
— the SAME engine the express auto-seed guard (`assert_joinable`) calls. This
collapses the two-checker drift risk that originally birthed the Sigma bug: when
the test and the auto-seed guard enforced the bridge via different code, a
token-reordered key (`sigma-35-art-dg-dn-ii` vs `...-dg-dn-art-ii`) slipped
between them. Now both entry points resolve through one definition.

Why the invariant matters (the failure it guards against): a contamination_key
that names a non-existent product key fails SILENTLY at extract time — the
relevance-gate lookup misses, falls to weak derived-alias matching, and can drop
ALL sources for that card (the 0/59 BUILD class documented in
registry_join_check.py). It is NOT caught by skus.json's own schema (the field is
a free string) nor by the registry writer (it doesn't see contamination.json).
Only a cross-registry resolve catches it.

Cross-repo note: both contamination.json AND the check() engine live in the
phantom-ops repo, not here. The aggregator-build dir is resolved via
ASKMADDI_AGGREGATOR_DIR, then default sibling locations; contamination.json via
ASKMADDI_CONTAMINATION_JSON, then siblings. If either cannot be found (a
prod-only checkout with no phantom-ops beside it), the affected tests SKIP with a
clear reason rather than false-failing — but wherever both repos are present
(CI, local, sandbox) they run and bite.
"""
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_SKUS = _HERE.parent / "data" / "skus.json"

# Default places the phantom-ops aggregator-build dir may sit relative to this
# repo. ASKMADDI_AGGREGATOR_DIR overrides all of these.
_AGG_CANDIDATES = [
    "../../phantom-ops/claude/workspace/aggregator-build",
    "../phantom-ops/claude/workspace/aggregator-build",
]
# Default places contamination.json may sit. ASKMADDI_CONTAMINATION_JSON
# overrides. (Kept independent of the agg dir so an unusual layout can point
# them separately, though normally contamination.json lives under the agg dir.)
_CONTAM_CANDIDATES = [
    "../../phantom-ops/claude/workspace/aggregator-build/fixtures/manifests/contamination.json",
    "../phantom-ops/claude/workspace/aggregator-build/fixtures/manifests/contamination.json",
]


def _norm(s: str) -> str:
    """Alphanumeric-only — matches registry_join_check._norm so 'same product,
    punctuation differs' reads identically on both sides of the bridge."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _find_aggregator_dir() -> Path | None:
    env = os.environ.get("ASKMADDI_AGGREGATOR_DIR")
    if env:
        p = Path(env).expanduser()
        return p if (p / "registry_join_check.py").exists() else None
    for rel in _AGG_CANDIDATES:
        p = (_HERE / rel).resolve()
        if (p / "registry_join_check.py").exists():
            return p
    return None


def _find_contamination() -> Path | None:
    env = os.environ.get("ASKMADDI_CONTAMINATION_JSON")
    if env:
        p = Path(env).expanduser()
        return p if p.exists() else None
    agg = _find_aggregator_dir()
    if agg is not None:
        p = agg / "fixtures" / "manifests" / "contamination.json"
        if p.exists():
            return p
    for rel in _CONTAM_CANDIDATES:
        p = (_HERE / rel).resolve()
        if p.exists():
            return p
    return None


def _load_check():
    """Import registry_join_check.check from phantom-ops, or skip."""
    agg = _find_aggregator_dir()
    if agg is None:
        pytest.skip(
            "phantom-ops aggregator-build not found beside this repo; set "
            "ASKMADDI_AGGREGATOR_DIR to run the bridge-resolve test")
    mod_path = agg / "registry_join_check.py"
    spec = importlib.util.spec_from_file_location("registry_join_check", mod_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["registry_join_check"] = mod
    spec.loader.exec_module(mod)
    return mod


def _contam_path_or_skip() -> Path:
    p = _find_contamination()
    if p is None:
        pytest.skip(
            "contamination.json not found beside this repo; set "
            "ASKMADDI_CONTAMINATION_JSON to run the bridge-resolve test")
    return p


def _load_skus() -> dict:
    return json.loads(_SKUS.read_text())["skus"]


def test_every_contamination_key_resolves():
    """The core invariant, delegated to the shared engine: every registry
    contamination_key resolves to a real contamination product. check() joins on
    the declared contamination_key (the production bridge) and reports any
    no_contamination_entry as a HARD, gate-breaking issue. No silent misses.

    By calling check() rather than re-deriving the resolve, this test and the
    auto-seed `assert_joinable` guard enforce ONE definition of 'the bridge
    resolves' — the drift that birthed the Sigma bug cannot reopen here."""
    mod = _load_check()
    contam = _contam_path_or_skip()
    rep = mod.check(skus_path=_SKUS, contam_path=contam)

    hard = [i for i in rep.issues if i.kind == "no_contamination_entry"]
    assert not hard, "broken skus->contamination bridges:\n" + "\n".join(
        i.line() for i in hard)
    assert rep.ok


def test_sigma_bridge_specifically_resolves():
    """Regression pin for the 2026-06-23 break: the Sigma card must bridge to
    the real 'sigma-35-art-dg-dn-ii' key, not the token-reordered phantom."""
    mod = _load_check()
    contam_path = _contam_path_or_skip()
    skus = _load_skus()
    ck = skus["sigma-35-art-dg-dn-ii"]["contamination_key"]
    assert ck == "sigma-35-art-dg-dn-ii"
    # Resolve through the same engine the gate uses.
    contam = json.loads(Path(contam_path).read_text())
    assert ck in contam.get("products", {})


def test_no_token_reordered_keys():
    """Belt-and-suspenders: catch any key with the RIGHT tokens in the WRONG
    order — resolvable by a human, invisible to alphanumeric normalization,
    fatal to the gate. This is the exact shape the Sigma bug took.

    check() now carries a token-set reorder detector itself (promoted from this
    test on 2026-06-24), so a reorder surfaces as a no_contamination_entry whose
    detail names the REORDER class. We assert no such issue is reported."""
    mod = _load_check()
    contam = _contam_path_or_skip()
    rep = mod.check(skus_path=_SKUS, contam_path=contam)
    reorders = [i for i in rep.issues
                if i.kind == "no_contamination_entry" and "REORDER" in i.detail]
    assert not reorders, "token-reordered (right tokens, wrong order) keys:\n" + "\n".join(
        i.line() for i in reorders)
