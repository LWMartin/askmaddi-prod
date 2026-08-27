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

# Cap on Used listings in the canonical section, so eBay's many unique-titled
# listings of one body can't flood the page after the New lanes exhaust.
USED_CANONICAL_CAP = 12

# lazy singletons
_identity_lookup = None


def _condition_class(row):
    return "new" if (row.get("condition") or "").strip().lower() == "new" else "used"


def _round_robin(lanes):
    """Take one row from each lane in order, cycling until all lanes are empty.
    Empty/exhausted lanes are skipped. Each lane keeps its own relevance order.
    Order of `lanes` defines the surfacing priority within each cycle."""
    out = []
    idx = [0] * len(lanes)
    remaining = sum(len(l) for l in lanes)
    while remaining:
        for k, lane in enumerate(lanes):
            if idx[k] < len(lane):
                out.append(lane[idx[k]])
                idx[k] += 1
                remaining -= 1
    return out


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

    # Surface a source x condition MIX. eBay lists MANY unique-titled listings of
    # one body (never dedup) while Adorama has a few clean rows, so raw relevance
    # buries the New/Adorama buy-path under eBay's Used volume. Round-robin four
    # lanes so the top of the page mixes both sources AND both conditions — New
    # from each retailer plus a Used comparison right up top. Each lane stays
    # relevance-ranked; Used is capped so it can't flood after New exhausts.
    # (Adorama-Used is empty in practice — the feed is retail New — so the live
    # cycle is Adorama-New -> eBay-New -> eBay-Used, repeating.)
    def _lane(seller_is_adorama, cond):
        return [r for r in canonical
                if (r.get("seller") == "Adorama") == seller_is_adorama
                and _condition_class(r) == cond]
    adorama_new, ebay_new = _lane(True, "new"), _lane(False, "new")
    adorama_used, ebay_used = _lane(True, "used"), _lane(False, "used")[:USED_CANONICAL_CAP]
    canonical = _round_robin([adorama_new, ebay_new, adorama_used, ebay_used])

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
