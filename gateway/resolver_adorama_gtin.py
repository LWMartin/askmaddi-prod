"""Adorama/Partnerize GTIN feed resolver (rung E) — airlocked gateway cell.

Woken from the factory STUB (`aggregator-build/resolver/resolver_adorama_gtin.py`,
spec maddi-multisource-identity-matcher §Cells) now that the rows have landed. The
factory cell leaned on phantom-ops `ingest.product_feed` for column mapping; that
module is NOT in the gateway airlock, and it is not needed here — the nightly
`ingest.adorama_search_index` already writes the feed PRE-NORMALISED to
`data/adorama-search-index.json` with the locked columns {brand, gtin, image,
model, mpn, price, url}. So this cell reads that index directly (the spec's
"column aliases locked when the real headers arrive" step).

Rung E is the narrowest source (only Adorama's in-stock New catalogue), so the
orchestrator runs it LAST (A->E). Its distinct value over rungs C/D: a match
carries the Partnerize-wrapped **buyable url** (the CTA + herald lane) alongside a
deterministic GTIN/MPN identity. Empty/missing index -> None (stub posture
preserved), so the orchestrator escalates to the true unmet floor cleanly.

Identity normalisation reuses skus_registry (`_is_placeholder_mpn` /
`_norm_join_mpn`) so a rung-E join compares equal to the id-gate and the dedup
join — one placeholder arbiter, one MPN shape across the whole matcher.
"""
from __future__ import annotations

import json
import os
import re
import threading
from typing import Optional

import skus_registry

_INDEX_PATH = os.environ.get(
    "ADORAMA_SEARCH_INDEX",
    os.path.join(os.path.dirname(__file__), "..", "data", "adorama-search-index.json"))

_lock = threading.Lock()
_cache = {"mtime": None, "rows": []}


def is_configured():
    return os.path.exists(_INDEX_PATH)


def _load_rows(index_path=None):
    """(Re)load the normalised in-stock index if the file changed; [] if absent.

    Mirrors adorama_index._load's mtime-cache so the two Adorama consumers share
    the refresh discipline (the nightly rewrites the file atomically)."""
    path = index_path or _INDEX_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    with _lock:
        if _cache["mtime"] == mtime and index_path is None:
            return _cache["rows"]
        with open(path, encoding="utf-8") as fh:
            rows = json.load(fh).get("rows", [])
        if index_path is None:
            _cache.update(mtime=mtime, rows=rows)
        return rows


def _canon_gtin(x):
    """Digits, zero-padded to GTIN-14 — a feed GTIN-12/13 compares equal to the
    same product's GTIN-14 (matches resolve_sku._norm_gtin). Empty -> '' (skip)."""
    d = re.sub(r"\D", "", str(x or ""))
    return d.zfill(14) if d else ""


def _join_mpn(x):
    """Alphanumeric-upper MPN, placeholder stripped to '' (never a join key).
    Single arbiter: skus_registry._is_placeholder_mpn (mirrors resolve_sku)."""
    if skus_registry._is_placeholder_mpn(x):
        return ""
    return skus_registry._norm_join_mpn(x)


def _brand_agrees(row_brand, target_brand):
    """Lenient brand gate for an MPN join: either side empty passes; else one
    brand's first token must contain the other's (case-insensitive). Guards the
    MPN fallback against a cross-brand part-number collision without demanding an
    exact brand-string match ('Sony' vs 'Sony Electronics')."""
    a = (row_brand or "").strip().lower()
    b = (target_brand or "").strip().lower()
    if not a or not b:
        return True
    ta, tb = a.split()[0], b.split()[0]
    return ta in tb or tb in ta


def resolve(target, *, index=None, index_path=None):
    """Resolve target identity from the Adorama in-stock index (rung E).

    Args:
        target: product with keys vendor/brand, model, gtin (opt), mpn (opt).
        index:  injected list of normalised rows (tests); None -> load from file.
        index_path: override file path (tests); None -> cached default index.

    Returns SourceResolution dict (keys: source, identity, confidence,
    deterministic, aliases, relations, raw, buyable_url, why) or None.

    Join order: GTIN exact (deterministic) -> brand-scoped MPN exact
    (deterministic) -> brand+model (only when the target carries NO join key,
    probabilistic). A target that DOES carry a join key but finds no exact hit
    returns None (never a brand+model guess) so a real miss escalates truthfully.
    """
    rows = index if index is not None else _load_rows(index_path)
    if not rows:
        return None

    t_gtin = _canon_gtin(target.get("gtin"))
    t_mpn = _join_mpn(target.get("mpn"))
    t_brand = (target.get("vendor") or target.get("brand") or "").strip()
    t_model = (target.get("model") or target.get("canonical_model") or "").strip()

    if t_gtin or t_mpn:
        if t_gtin:
            for row in rows:
                if _canon_gtin(row.get("gtin")) == t_gtin:
                    return _make(row, deterministic=True, confidence=1.0,
                                 why="Adorama GTIN exact")
        if t_mpn:
            for row in rows:
                r_mpn = _join_mpn(row.get("mpn"))
                if r_mpn and r_mpn == t_mpn and _brand_agrees(row.get("brand"), t_brand):
                    return _make(row, deterministic=True, confidence=1.0,
                                 why="Adorama MPN exact (brand-scoped)")
        return None  # carried a join key, no exact hit -> escalate, never guess

    if t_brand and t_model:
        for row in rows:
            if (row.get("brand", "").strip().lower() == t_brand.lower()
                    and row.get("model", "").strip().lower() == t_model.lower()):
                return _make(row, deterministic=False, confidence=0.85,
                             why="Adorama brand+model match")
    return None


def _make(row, *, deterministic, confidence, why):
    """Build a SourceResolution from a normalised index row. `buyable_url` is the
    Partnerize-wrapped feed link (already wrapped — never re-wrap) that gives
    rung E its distinct CTA/herald value; relations stay empty-shaped so the
    orchestrator's contamination-emit merges every source uniformly."""
    return {
        "source": "adorama",
        "identity": {
            "gtin": row.get("gtin", ""),
            "mpn": row.get("mpn", ""),
            "brand": row.get("brand", ""),
            "canonical_model": row.get("model", ""),
            "image": row.get("image"),
        },
        "confidence": confidence,
        "deterministic": deterministic,
        "aliases": [],
        "relations": {"predecessor": [], "competitor": []},
        "raw": {k: row.get(k) for k in ("brand", "model", "gtin", "mpn", "price", "url", "image")},
        "buyable_url": row.get("url"),
        "why": why,
    }
