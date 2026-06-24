#!/usr/bin/env python3
"""Integration test: the skus.json -> contamination.json bridge RESOLVES.

maddi-skus-registry build step 5 ("contamination_key bridge resolves"). This is
the test with teeth: it asserts every `contamination_key` in the live registry
points at a real product key in the editorial contamination registry.

Why it matters (the failure it guards against): a contamination_key that names a
non-existent product key fails SILENTLY at extract time — the relevance-gate
lookup misses, falls to weak derived-alias matching, and can drop ALL sources
for that card (the 0/59 BUILD class documented in registry_join_check.py). It is
NOT caught by skus.json's own schema (the field is a free string) nor by the
registry writer (it doesn't see contamination.json). Only a cross-registry
resolve catches it. This test found exactly that on 2026-06-23: the Sigma card's
key was 'sigma-35-dg-dn-art-ii' (token-reordered) with no such contamination
product — a real broken bridge in shipped data, fixed alongside this test.

Cross-repo note: contamination.json lives in the phantom-ops repo, not here. The
path is resolved via ASKMADDI_CONTAMINATION_JSON, then a few default sibling
locations. If it cannot be found (a prod-only checkout with no phantom-ops
beside it), the test SKIPS with a clear reason rather than false-failing — but
wherever both repos are present (CI, local, sandbox) it runs and bites.
"""
import json
import os
import re
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_SKUS = _HERE.parent / "data" / "skus.json"

# Default places contamination.json may sit relative to this repo. The env var
# ASKMADDI_CONTAMINATION_JSON overrides all of these.
_CONTAM_CANDIDATES = [
    "../phantom-ops/claude/workspace/aggregator-build/fixtures/manifests/contamination.json",
    "../../phantom-ops/claude/workspace/aggregator-build/fixtures/manifests/contamination.json",
]


def _norm(s: str) -> str:
    """Alphanumeric-only — matches registry_join_check._norm so 'same product,
    punctuation differs' reads identically on both sides of the bridge."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _find_contamination() -> Path | None:
    env = os.environ.get("ASKMADDI_CONTAMINATION_JSON")
    if env:
        p = Path(env).expanduser()
        return p if p.exists() else None
    for rel in _CONTAM_CANDIDATES:
        p = (_HERE / rel).resolve()
        if p.exists():
            return p
    return None


def _load_contamination_keys() -> set[str]:
    p = _find_contamination()
    if p is None:
        pytest.skip(
            "contamination.json not found beside this repo; set "
            "ASKMADDI_CONTAMINATION_JSON to run the bridge-resolve test")
    data = json.loads(p.read_text())
    products = data.get("products", data)
    return set(products.keys())


def _load_skus() -> dict:
    return json.loads(_SKUS.read_text())["skus"]


def test_every_contamination_key_resolves():
    """The core invariant: every registry contamination_key is a real product
    key in contamination.json. No silent misses."""
    contam = _load_contamination_keys()
    skus = _load_skus()

    broken = []
    for slug, entry in skus.items():
        ck = entry.get("contamination_key")
        if not ck:
            broken.append((slug, ck, "missing contamination_key"))
            continue
        if ck not in contam:
            # Token-set diagnostic surfaces the reorder/typo class specifically
            # (the Sigma 'art-dg-dn' vs 'dg-dn-art' case _norm alone misses).
            same_tokens = [c for c in contam if set(c.split("-")) == set(ck.split("-"))]
            same_norm = [c for c in contam if _norm(c) == _norm(ck)]
            hint = same_tokens or same_norm or ["<no near match>"]
            broken.append((slug, ck, f"no such contamination product; did you mean {hint}?"))

    assert not broken, "broken skus->contamination bridges:\n" + "\n".join(
        f"  {slug}: contamination_key={ck!r} — {why}" for slug, ck, why in broken)


def test_sigma_bridge_specifically_resolves():
    """Regression pin for the 2026-06-23 break: the Sigma card must bridge to
    the real 'sigma-35-art-dg-dn-ii' key, not the token-reordered phantom."""
    contam = _load_contamination_keys()
    skus = _load_skus()
    ck = skus["sigma-35-art-dg-dn-ii"]["contamination_key"]
    assert ck == "sigma-35-art-dg-dn-ii"
    assert ck in contam


def test_no_token_reordered_keys():
    """Belt-and-suspenders: catch any key that has the RIGHT tokens in the WRONG
    order — resolvable by a human, invisible to alphanumeric normalization,
    fatal to the gate. This is the exact shape the Sigma bug took."""
    contam = _load_contamination_keys()
    skus = _load_skus()
    contam_by_tokens = {}
    for c in contam:
        contam_by_tokens.setdefault(frozenset(c.split("-")), []).append(c)

    offenders = []
    for slug, entry in skus.items():
        ck = entry.get("contamination_key") or ""
        if ck in contam:
            continue
        tok = frozenset(ck.split("-"))
        if tok in contam_by_tokens:
            offenders.append((slug, ck, contam_by_tokens[tok]))

    assert not offenders, "token-reordered (right tokens, wrong order) keys:\n" + "\n".join(
        f"  {slug}: {ck!r} should be one of {opts}" for slug, ck, opts in offenders)
