"""Lane A orchestrator — the /search precise-product-research path.

Fans out the two sanctioned sources server-side (eBay Browse = Used lane,
Adorama feed index = New lane), runs the vendored Sieve cells, and returns one
sectioned payload the frontend renders directly. This SUPERSEDES the client-side
streamSearch fan-out for the precise route; the legacy /ebay/search stays intact
so the switch is reversible.

v1 rungs: 0 (classify against the carded spine + accessory markers), 1 (lexical
rerank), dedup-by-identity, compose (canonical-first + capped compatible tail).
Rung 2 (Qwen3 forced-choice on a bounded ambiguous residue) is present in
search_cells.arbitrate but intentionally NOT wired into the hot path in v1 — it
adds a per-search model dependency; enabling it over a small top-N residue is the
documented precision fast-follow. Condition is a per-row facet (both sources span
conditions), so there is no source-based section split.
"""
from __future__ import annotations

import search_cells

# lazy singletons
_identity_lookup = None


def _lookup():
    global _identity_lookup
    if _identity_lookup is None:
        import search_identity
        _identity_lookup = search_identity.build_identity_lookup()
    return _identity_lookup


def _gather(query, limit):
    """Fan out both sources; attach the identity sidecar to each row."""
    rows = []
    # Adorama (New) — already carries {gtin,mpn,brand,model}
    try:
        import adorama_index
        if adorama_index.is_configured():
            rows.extend(adorama_index.search(query, limit=limit))
    except Exception as e:  # a dead source never blocks the other
        print(f"[search] adorama source error: {e}")
    # eBay (Used/varies) — thin rows; sidecar is best-effort (no GTIN in summary)
    try:
        import ebay_api
        if ebay_api.is_configured():
            for it in ebay_api.search(query, limit=limit):
                it.setdefault("gtin", None)
                it.setdefault("mpn", None)
                it.setdefault("brand", None)
                it.setdefault("model", None)
                rows.append(it)
    except Exception as e:
        print(f"[search] ebay source error: {e}")
    return rows


def precise_search(query, limit=25):
    """Run the Lane A Sieve over both sources. Returns the compose() payload
    plus a diagnostics block."""
    query = (query or "").strip()
    if not query:
        return {"results": [], "sections": [], "dropped_tail": 0,
                "diagnostics": {"query": "", "sources": {}}}

    rows = _gather(query, limit)
    lookup = _lookup()

    canonical, accessory = [], []
    n_resolved = 0
    for row in rows:
        verdict = search_cells.classify_result(row, lookup)
        klass = verdict["klass"]
        if klass == "canonical":
            row = {**row, "identity_key": verdict["identity_key"], "carded": True}
            n_resolved += 1
            canonical.append(row)
        elif klass == "accessory":
            accessory.append(row)
        else:  # ambiguous → canonical-eligible (real-looking), ranked by relevance
            canonical.append({**row, "identity_key": None})

    canonical = search_cells.dedup_by_identity(canonical)
    canonical = search_cells.rerank(query, canonical)
    accessory = search_cells.rerank(query, accessory)

    payload = search_cells.compose(canonical, accessory, tail_cap=8)
    payload["diagnostics"] = {
        "query": query,
        "sources": {
            "adorama": sum(1 for r in rows if r.get("seller") == "Adorama"),
            "ebay": sum(1 for r in rows if r.get("seller") not in (None, "Adorama")),
        },
        "resolved_to_spine": n_resolved,
        "accessory_tail": len(accessory),
    }
    return payload
