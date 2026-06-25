#!/usr/bin/env python3
"""Cross-repo agreement: slug_common must match the phantom-ops originals.

This is the linchpin of the copy-with-agreement decision (2026-06-24). When
slug_normalizer graduated into askmaddi-prod, its two phantom-ops dependencies
— `ingest.adapters._common.slugify` and `registry_join_check._norm` — were
COPIED into the in-repo `slug_common` module rather than imported across the
repo boundary (a cross-repo import / subprocess shell-out is a liability in the
gateway hot path the live writer will eventually run in).

Duplication of a frozen primitive is only safe if it cannot silently drift. This
test converts "don't let them drift" from a hope into a red CI run: it asserts
`slug_common.slugify` and `slug_common._norm` produce BYTE-IDENTICAL output to
the phantom-ops originals across a corpus of slugs spanning the live cadre, the
known collision classes (sony-a7iv ~ sony-a7-iv), token reorders, punctuation,
unicode, and truncation boundaries.

Cross-repo note (mirrors test_contamination_bridge.py): the originals live in
phantom-ops, not here. The aggregator-build dir is resolved via
ASKMADDI_AGGREGATOR_DIR, then default sibling locations. On a prod-only checkout
with no phantom-ops beside it, these tests SKIP with a clear reason rather than
false-failing — but wherever both repos are present (CI, local, sandbox) they
run and bite. A drift in either frozen primitive turns this red.
"""
# PEP 604 union syntax (`Path | None` below) deferred to a string under this
# import, so it parses on the VPS's Python 3.9 (same fix + rationale as
# test_contamination_bridge.py — the 3.9-vs-3.12 gate blind spot caught 2026-06-25).
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent

# Match test_contamination_bridge.py's discovery convention exactly.
_AGG_CANDIDATES = [
    "../../phantom-ops/claude/workspace/aggregator-build",
    "../phantom-ops/claude/workspace/aggregator-build",
]


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


def _load_module(path: Path, name: str):
    """Import a module from an explicit file path, isolated under a unique name
    so it does not collide with the in-repo modules of the same base name."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# The corpus: every input where a frozen-rule divergence would actually bite.
# Cadre identities, collision classes, reorders, punctuation, unicode, length.
_SLUGIFY_CORPUS = [
    ("Sony A7 IV (ILCE-7M4)", 60),
    ("Sigma 35mm f/1.4 DG DN Art II", 60),
    ("Sigma 35mm f/1.4 Art", 60),
    ("Peak Design Pro Tripod", 60),
    ("Peak Design Travel Tripod", 60),
    ("Tamron 28-75mm f/2.8 G2", 60),
    ("Nikon Z6 III", 60),
    ("  leading and trailing   ", 60),
    ("Caf\u00e9 R\u00e9sum\u00e9 na\u00efve", 60),
    ("UPPER lower MiXeD", 60),
    ("punctuation!!! @#$ %^&*() slug", 60),
    ("a" * 80, 60),                       # truncation boundary
    ("trailing-hyphen-after-cut-" + "x" * 50, 30),  # no trailing hyphen post-cut
    ("multiple---hyphens___and   spaces", 60),
    ("", 60),                             # degenerate
    ("123 456 789", 60),
]

_NORM_CORPUS = [
    "sony-a7iv", "sony-a7-iv", "Sony A7 IV",
    "sigma-35-art-dg-dn-ii", "sigma-35-art-dg-dn-art-ii",
    "sigma-35-dg-dn-art-ii",             # token reorder class
    "Peak Design Pro Tripod",
    "UPPER-lower-MiXeD",
    "punctuation!!!@#$%^&*()",
    "Caf\u00e9 R\u00e9sum\u00e9",
    "  spaced  ", "", "123-456",
]


@pytest.fixture(scope="module")
def phantom_originals():
    agg = _find_aggregator_dir()
    if agg is None:
        pytest.skip(
            "phantom-ops aggregator-build not found beside repo "
            "(set ASKMADDI_AGGREGATOR_DIR) — agreement test skipped on "
            "prod-only checkout."
        )
    # slugify lives in ingest/adapters/_common.py; _norm in registry_join_check.py.
    common = _load_module(
        agg / "ingest" / "adapters" / "_common.py", "_phantom_common")
    # registry_join_check imports nothing heavy at module load for _norm's sake,
    # but it does define module-level fixture Paths — loading by file path is fine.
    rjc = _load_module(agg / "registry_join_check.py", "_phantom_rjc")
    return {"slugify": common.slugify, "_norm": rjc._norm}


def test_slugify_agrees_with_phantom_ops(phantom_originals):
    import slug_common
    ref = phantom_originals["slugify"]
    for text, max_len in _SLUGIFY_CORPUS:
        assert slug_common.slugify(text, max_len) == ref(text, max_len), (
            f"slugify drift on {text!r} (max_len={max_len}): "
            f"prod={slug_common.slugify(text, max_len)!r} "
            f"phantom={ref(text, max_len)!r}"
        )


def test_norm_agrees_with_phantom_ops(phantom_originals):
    import slug_common
    ref = phantom_originals["_norm"]
    for s in _NORM_CORPUS:
        assert slug_common._norm(s) == ref(s), (
            f"_norm drift on {s!r}: "
            f"prod={slug_common._norm(s)!r} phantom={ref(s)!r}"
        )
