#!/usr/bin/env python3
"""
eBay second-pass GTIN probe — recover GTIN for the NO-GTIN tail via search.

WHY (the reframing that retired L2-Wikidata)
--------------------------------------------
The NO-GTIN tail is NO-GTIN because those specific listings are used /
one-of-a-kind and the seller didn't catalog-associate them, so eBay's
product.gtins is empty ON THAT LISTING. But the SAME physical product almost
certainly exists as a catalog-associated (ePID-bearing) listing elsewhere on
eBay — a different seller, often better-listed — and eBay's catalog carries
the GTIN on THAT record. We already hold brand + mpn for each tail SKU.

So instead of an external source (Wikidata is keyed GTIN->product, sparse on
consumer used-goods long-tail, and currently endpoint-degraded; GS1 canonical
lookup is also GTIN->product and $6,500/yr), we do a SECOND PASS AGAINST EBAY
ITSELF: search Browse by brand+mpn, find any catalog-associated result for the
same product, read the GTIN off THAT record's product.gtins.

Strictly better for THIS tail on every axis: it's the marketplace that indexes
used goods, keyed the direction we need (brand+mpn -> product -> GTIN), it's our
own entitled eBay access (doctrine-clean, no proxy, no carried dependency), and
the search path already exists and is proven (search / _search_candidates /
resolve in gateway/ebay_api.py; refresh_used_prices.py already searches Browse).

TRUST DISCIPLINE (inherited from L1, non-negotiable)
----------------------------------------------------
A brand+mpn search can return the WRONG product (accessories, compatible-with,
wrong variant). We must NOT blindly accept the first ePID's GTIN. This probe:
  - only considers CATALOG-ASSOCIATED candidates (ePID present)
  - re-resolves each via the live PRODUCT-fieldgroup path (same as L1)
  - reports EVERY GTIN found + whether they AGREE across candidates
  - never silently picks; a disagreement is a CONFLICT flagged for /admin
Same posture as L1's gtin_provenance conflict flag: clean signal when present,
flag conflict, abstain to human.

Read-only. No skus.json write, no resolve/extract change. Pure inspection.
"""
import json
import sys
import time

# --- env FIRST, before any transitive ebay_api import (L1's latent-bug fix) ---
sys.path.insert(0, "gateway")
import env_bootstrap  # noqa: E402
env_bootstrap.load_dotenv()

import ebay_api  # noqa: E402

# Reuse the L1 probe's live-resolve classification as the single source of
# truth for which SKUs are NO-GTIN. No duplicated resolve logic.
sys.path.insert(0, "tools")
import probe_gtin_in_payload as l1  # noqa: E402


def _candidate_gtin(item_id):
    """Re-resolve a search candidate via the live PRODUCT path; return the
    extracted gtin + provenance (same fields L1 wired into identity)."""
    try:
        r = ebay_api.resolve(item_id)
    except Exception as e:
        return {"item_id": item_id, "error": str(e)}
    idn = r.get("identity", {})
    return {
        "item_id": item_id,
        "gtin": idn.get("gtin"),
        "chosen_source": (idn.get("gtin_provenance") or {}).get("chosen_source"),
        "conflict": (idn.get("gtin_provenance") or {}).get("conflict"),
    }


def probe_sku(slug, entry):
    """Search brand+mpn, resolve catalog-associated candidates, collect GTINs."""
    ident = entry.get("identity", {})
    brand = (ident.get("brand") or "").strip()
    mpn = (ident.get("mpn") or "").strip()
    model = (entry.get("model") or ident.get("market_title") or "").strip()

    # Prefer brand+mpn (strongest key); fall back to brand+model if mpn absent.
    if brand and mpn:
        query = f"{brand} {mpn}"
        key = "brand+mpn"
    elif brand and model:
        query = f"{brand} {model}"
        key = "brand+model (no mpn)"
    else:
        return {"slug": slug, "query": None, "key": "insufficient-keys",
                "candidates": [], "gtins": [], "verdict": "NO-KEYS"}

    try:
        cands = ebay_api._search_candidates(query, limit=10)
    except Exception as e:
        return {"slug": slug, "query": query, "key": key,
                "candidates": [], "gtins": [], "verdict": f"SEARCH-FAILED: {e}"}

    # Only catalog-associated candidates (ePID present) can carry a catalog GTIN.
    assoc = [c for c in cands if c.get("epid")]
    results = []
    gtins = set()
    for c in assoc[:5]:  # cap resolves; be polite to the API
        res = _candidate_gtin(c["item_id"])
        res["epid"] = c.get("epid")
        res["title"] = c.get("title", "")[:70]
        results.append(res)
        if res.get("gtin"):
            gtins.add(res["gtin"])
        time.sleep(0.5)

    if not cands:
        verdict = "NO-SEARCH-RESULTS"
    elif not assoc:
        verdict = "NO-CATALOG-ASSOC"      # results exist but none ePID-bearing
    elif not gtins:
        verdict = "ASSOC-BUT-NO-GTIN"     # catalog-associated, still no product.gtins
    elif len(gtins) == 1:
        verdict = "GTIN-RECOVERED"        # clean single GTIN across candidates
    else:
        verdict = "GTIN-CONFLICT"         # multiple disagreeing GTINs -> /admin

    return {"slug": slug, "query": query, "key": key,
            "n_results": len(cands), "n_assoc": len(assoc),
            "candidates": results, "gtins": sorted(gtins), "verdict": verdict}


def main():
    skus_path = sys.argv[1] if len(sys.argv) > 1 else "data/skus.json"
    reg = json.load(open(skus_path))
    skus = reg.get("skus", reg)

    print("=" * 64)
    print("  EBAY SECOND-PASS GTIN PROBE — NO-GTIN tail via brand+mpn search")
    print("  read-only; recovers catalog GTIN from a DIFFERENT listing of the")
    print("  same product; conflict-flagged, never silent-pick")
    print("=" * 64)

    # 1) derive the NO-GTIN tail from the LIVE resolve path (single source of truth)
    no_gtin = []
    for slug, entry in skus.items():
        _s, verdict, _d = l1.inspect(slug, entry)
        if verdict.split()[0] == "NO-GTIN":
            no_gtin.append((slug, entry))
    print(f"\nNO-GTIN tail (live-resolve verdict): {len(no_gtin)} SKU(s)\n")

    summary = {}
    for i, (slug, entry) in enumerate(no_gtin, 1):
        r = probe_sku(slug, entry)
        summary.setdefault(r["verdict"].split(":")[0], []).append(slug)
        print(f"=== [{i}/{len(no_gtin)}] {slug} ===")
        print(f"    query:   {r['query']!r}  ({r['key']})")
        if "n_results" in r:
            print(f"    results: {r['n_results']} total, {r['n_assoc']} catalog-associated (ePID)")
        for c in r.get("candidates", []):
            if c.get("error"):
                print(f"      - {c['item_id']}  ERROR {c['error']}")
            else:
                print(f"      - epid={c.get('epid')}  gtin={c.get('gtin')}  "
                      f"src={c.get('chosen_source')}  \"{c.get('title','')}\"")
        if r.get("gtins"):
            print(f"    GTIN(s): {', '.join(r['gtins'])}")
        print(f"    VERDICT: {r['verdict']}\n")

    print("-" * 64)
    print("RECOVERY SUMMARY (of the NO-GTIN tail):")
    for v in ("GTIN-RECOVERED", "GTIN-CONFLICT", "ASSOC-BUT-NO-GTIN",
              "NO-CATALOG-ASSOC", "NO-SEARCH-RESULTS", "NO-KEYS", "SEARCH-FAILED"):
        got = summary.get(v, [])
        if got:
            print(f"  {v:18s} {len(got):2d}  {', '.join(got)}")
    n = len(no_gtin)
    recovered = len(summary.get("GTIN-RECOVERED", []))
    conflict = len(summary.get("GTIN-CONFLICT", []))
    print("-" * 64)
    print(f"Clean GTIN recovery: {recovered}/{n}"
          f"   (+{conflict} conflict -> /admin review)")
    print("  This is the number that sizes the eBay second-pass build vs.")
    print("  letting the residual tail fall to the substrate's Gemma+title")
    print("  fallback (the designed behavior for used/one-of-a-kind goods).")


if __name__ == "__main__":
    main()
