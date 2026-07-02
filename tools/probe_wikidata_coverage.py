#!/usr/bin/env python3
"""
Wikidata L2 coverage probe — read-only measurement of the NO-GTIN tail.

WHY
---
L1 (eBay product.gtins extraction) is live and proven. It leaves a tail of
SKUs whose eBay listings are used / not catalog-associated, so eBay carries
no GTIN for them. Wikidata is the doctrine-clean (CC0) external complement,
keyed by the brand + mpn we already hold regardless of listing condition.

Before building any SPARQL client / L2 resolve path, this probe answers the
ONLY question that sizes (or kills) the L2 build:

    For the NO-GTIN tail, does Wikidata actually carry these products AND
    give us a GTIN (P3962) for them?

A Wikidata hit with no P3962 is a MISS for L2's purpose — it doesn't feed
the identity axis. So "coverage" here means GTIN-yielding coverage, not
merely "is the product in Wikidata."

Per Lee (2026-07-01): probe BOTH lookup strategies and report each
separately, so we learn HOW Wikidata carries photography gear (by MPN as a
real identifier, vs only by fuzzy label), which shapes any eventual client.

DESIGN (grounded, verify-as-we-go)
----------------------------------
- The NO-GTIN tail is derived from the LIVE resolve() path, NOT from disk.
  We proved on-disk `identity.gtin` is 0/14 today (backfill is gated on
  re-resolution that hasn't happened), so a `gtin is null` disk filter would
  wrongly select all 14. Single source of truth = probe_gtin_in_payload's
  own `inspect()` verdict == "NO-GTIN".
- Read-only. No skus.json write, no resolve/extract change. Pure inspection,
  same posture as probe_gtin_in_payload.
- env_bootstrap.load_dotenv() BEFORE importing ebay_api (module-level cred
  read; the latent bug fixed last session). We import the L1 probe, which
  imports ebay_api, so we must load env first here too.

Read the report, paste it back. The numbers decide whether L2 is worth a
carried Wikidata dependency.
"""
import json
import sys
import time
import urllib.parse
import urllib.request

# --- env FIRST, before any transitive ebay_api import (see module docstring) ---
sys.path.insert(0, "gateway")
import env_bootstrap  # noqa: E402
env_bootstrap.load_dotenv()

# Reuse the L1 probe's live-resolve classification as the single source of
# truth for which SKUs are NO-GTIN. No duplicated resolve logic.
sys.path.insert(0, "tools")
import probe_gtin_in_payload as l1  # noqa: E402

WDQS = "https://query.wikidata.org/sparql"
UA = "AskMaddi-L2-coverage-probe/0.1 (https://askmaddi.com; identity-axis diligence)"
# CC0 endpoint; identify ourselves honestly per sourcing doctrine.

P_MANUFACTURER = "P176"
P_GTIN = "P3962"


def _sparql(query):
    """Run a SPARQL query, return parsed JSON bindings (list of dicts)."""
    url = WDQS + "?" + urllib.parse.urlencode({"query": query, "format": "json"})
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/sparql-results+json"})
    with urllib.request.urlopen(req, timeout=45) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data.get("results", {}).get("bindings", [])


def _esc(s):
    return (s or "").replace('"', '\\"').strip()


def probe_by_mpn(brand, mpn):
    """
    STRICT: find items whose value on any string property equals the MPN,
    optionally scoped by manufacturer brand. Report QID(s) and whether each
    carries a GTIN (P3962). MPN is treated as a strong identifier string.
    """
    mpn_e = _esc(mpn)
    if not mpn_e:
        return {"strategy": "mpn", "hits": [], "note": "no mpn on entry"}
    # Match items whose label/alias OR any external-id string equals the MPN.
    # We keep this deliberately broad on the property but exact on the value,
    # then read P3962 presence per hit.
    q = f'''
    SELECT DISTINCT ?item ?itemLabel ?gtin WHERE {{
      ?item ?p ?val .
      FILTER(STR(?val) = "{mpn_e}")
      OPTIONAL {{ ?item wdt:{P_GTIN} ?gtin. }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }} LIMIT 25
    '''
    rows = _sparql(q)
    return {"strategy": "mpn", "hits": _fold(rows)}


def probe_by_label(brand, model_text):
    """
    BROAD: full-text-ish label/alias match on "brand model". Noisier; catches
    products Wikidata carries only as named entities, not by MPN identifier.
    Report QID(s) + GTIN presence per hit.
    """
    term = _esc(f"{brand} {model_text}".strip())
    if not term:
        return {"strategy": "label", "hits": [], "note": "no brand/model on entry"}
    q = f'''
    SELECT DISTINCT ?item ?itemLabel ?gtin WHERE {{
      SERVICE wikibase:mwapi {{
        bd:serviceParam wikibase:api "EntitySearch" .
        bd:serviceParam wikibase:endpoint "www.wikidata.org" .
        bd:serviceParam mwapi:search "{term}" .
        bd:serviceParam mwapi:language "en" .
        ?item wikibase:apiOutputItem mwapi:item .
      }}
      OPTIONAL {{ ?item wdt:{P_GTIN} ?gtin. }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }} LIMIT 25
    '''
    rows = _sparql(q)
    return {"strategy": "label", "hits": _fold(rows)}


def _fold(rows):
    """Collapse SPARQL rows into per-QID hits with a gtins list."""
    by_qid = {}
    for row in rows:
        item = row.get("item", {}).get("value", "")
        qid = item.rsplit("/", 1)[-1] if item else "?"
        label = row.get("itemLabel", {}).get("value", "")
        gtin = row.get("gtin", {}).get("value")
        h = by_qid.setdefault(qid, {"qid": qid, "label": label, "gtins": []})
        if gtin and gtin not in h["gtins"]:
            h["gtins"].append(gtin)
    return list(by_qid.values())


def _verdict(mpn_res, label_res):
    """GTIN-yielding coverage verdict for one SKU."""
    mpn_gtin = any(h["gtins"] for h in mpn_res.get("hits", []))
    label_gtin = any(h["gtins"] for h in label_res.get("hits", []))
    mpn_hit = bool(mpn_res.get("hits"))
    label_hit = bool(label_res.get("hits"))
    if mpn_gtin:
        return "WD-GTIN-BY-MPN"        # best: strong identifier + GTIN
    if label_gtin:
        return "WD-GTIN-BY-LABEL"      # usable: GTIN but only via fuzzy label
    if mpn_hit or label_hit:
        return "WD-HIT-NO-GTIN"        # in Wikidata, but no P3962 -> no L2 help
    return "WD-MISS"                   # not in Wikidata at all


def main():
    skus_path = sys.argv[1] if len(sys.argv) > 1 else "data/skus.json"
    reg = json.load(open(skus_path))
    skus = reg.get("skus", reg)

    print("=" * 60)
    print("  WIKIDATA L2 COVERAGE PROBE — NO-GTIN tail")
    print("  read-only; no writes; two strategies reported separately")
    print("=" * 60)

    # 1) derive the NO-GTIN tail from the LIVE resolve path (single source of truth)
    no_gtin = []
    for slug, entry in skus.items():
        _slug, verdict, _detail = l1.inspect(slug, entry)
        if verdict.split()[0] == "NO-GTIN":
            no_gtin.append((slug, entry))
    print(f"\nNO-GTIN tail (live-resolve verdict): {len(no_gtin)} SKU(s)\n")

    summary = {}
    for slug, entry in no_gtin:
        ident = entry.get("identity", {})
        brand = ident.get("brand", "")
        mpn = ident.get("mpn", "")
        model = entry.get("model") or ident.get("market_title", "")
        print(f"=== {slug} ===  brand={brand!r} mpn={mpn!r}")

        mpn_res = probe_by_mpn(brand, mpn)
        time.sleep(1.0)  # be polite to WDQS
        label_res = probe_by_label(brand, model)
        time.sleep(1.0)

        v = _verdict(mpn_res, label_res)
        summary.setdefault(v, []).append(slug)

        def _show(res):
            if res.get("note"):
                return f"({res['note']})"
            if not res["hits"]:
                return "no hits"
            parts = []
            for h in res["hits"][:5]:
                g = ",".join(h["gtins"]) if h["gtins"] else "no-P3962"
                parts.append(f"{h['qid']}[{h['label']}]->{g}")
            return "; ".join(parts)

        print(f"    by-mpn:   {_show(mpn_res)}")
        print(f"    by-label: {_show(label_res)}")
        print(f"    VERDICT:  {v}\n")

    print("-" * 60)
    print("COVERAGE SUMMARY (of the NO-GTIN tail):")
    for v in ("WD-GTIN-BY-MPN", "WD-GTIN-BY-LABEL", "WD-HIT-NO-GTIN", "WD-MISS"):
        got = summary.get(v, [])
        print(f"  {v:18s} {len(got):2d}  {', '.join(got)}")
    n = len(no_gtin)
    gtin_yield = len(summary.get("WD-GTIN-BY-MPN", [])) + len(summary.get("WD-GTIN-BY-LABEL", []))
    print("-" * 60)
    print(f"GTIN-yielding coverage: {gtin_yield}/{n}")
    print("  (this is the number that sizes the L2 build; a WD hit with no")
    print("   P3962 does not feed the identity axis.)")


if __name__ == "__main__":
    main()
