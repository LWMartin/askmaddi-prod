#!/usr/bin/env python3
"""
probe_gtin_in_payload.py — does the eBay payload we already fetch carry a GTIN?

The substrate spec (step 5) assumed GTIN "rides the same PRODUCT fieldgroup path"
that already yields brand/mpn. _extract_identity() walks localizedAspects for
brand/mpn but extracts nothing else — so if a GTIN/UPC/EAN is sitting in those
same aspects (or in the product container), we are fetching it and discarding it
at the seam. This probe answers that empirically against the LIVE resolve() path,
so the answer reflects production, not a fixture.

For each minted SKU it re-resolves the item (from its legacy_item_id) and reports,
WITHOUT changing anything:
  - every localizedAspects name (so we see eBay's actual aspect vocabulary)
  - any aspect whose name looks like a product code (gtin/ean/upc/isbn/global trade)
  - product-container keys (gtin lives there in some payloads)
  - a verdict per SKU: GTIN-FOUND (where) / NO-GTIN / RESOLVE-FAILED

Run on box as askmaddi (owns the eBay env):
  sudo -u askmaddi bash -lc 'cd /home/askmaddi/askmaddi-prod && \
    python3 /path/probe_gtin_in_payload.py'

Read-only. No skus.json write, no _extract_identity change. Pure inspection.
"""
import json, os, sys, re

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "gateway"))
sys.path.insert(0, "gateway")
try:
    import ebay_api
    import skus_registry
except ImportError as e:
    print(f"FATAL: cannot import gateway modules ({e}). Run from askmaddi-prod root.")
    sys.exit(2)

CODE_RE = re.compile(r"gtin|ean|upc|isbn|global trade|barcode|product code", re.I)

def _re_resolve(legacy_id):
    """Reconstruct the RESTful item id and call the live resolve()."""
    item_id = f"v1|{legacy_id}|0"
    return ebay_api.resolve(item_id)

def inspect(slug, entry):
    idn = entry.get("identity", {}) or {}
    legacy = idn.get("legacy_item_id", "")
    if not legacy:
        return slug, "NO-LEGACY-ID", {}
    try:
        res = _re_resolve(legacy)
    except Exception as e:
        return slug, f"RESOLVE-FAILED ({type(e).__name__}: {e})", {}
    raw = res.get("_raw", {}) or {}
    product = raw.get("product", {}) or {}
    aspects = raw.get("localizedAspects", []) or []

    # PRIMARY (verified against eBay Browse API docs 2026-06-30): the PRODUCT
    # fieldgroup surfaces GTIN in two product-container locations —
    #   product.gtins                      (dedicated GTIN array)
    #   product.additionalProductIdentities (structured {type,value} identifiers)
    # Both are returned ONLY when the seller associated an ePID/catalog product;
    # used / one-of-a-kind listings carry neither (the substrate spec's named gap).
    primary_codes = {}
    if product.get("gtins"):
        primary_codes["product.gtins"] = product["gtins"]
    if product.get("additionalProductIdentities"):
        primary_codes["product.additionalProductIdentities"] = \
            product["additionalProductIdentities"]
    # legacy single-key fallbacks (older payload shapes)
    for k in ("gtin", "ean", "upc", "isbn"):
        if k in product:
            primary_codes[f"product.{k}"] = product[k]

    # SECONDARY: a GTIN-ish aspect occasionally rides localizedAspects.
    aspect_names = [a.get("name", "") for a in aspects]
    code_aspects = {a.get("name", ""): a.get("value", "")
                    for a in aspects if CODE_RE.search(a.get("name", "") or "")}

    product_codes = primary_codes  # keep downstream var name
    found = bool(primary_codes or code_aspects)
    if primary_codes:
        verdict = "GTIN-FOUND"            # in the authoritative location
    elif code_aspects:
        verdict = "GTIN-FOUND-ASPECT"     # only in aspects — weaker, still usable
    else:
        verdict = "NO-GTIN"               # seller didn't catalog-associate -> spec gap
    detail = {
        "all_aspect_names": aspect_names,
        "code_aspects": code_aspects,
        "product_codes": product_codes,
        "brand": idn.get("brand"), "mpn": idn.get("mpn"),
    }
    return slug, verdict, detail

def main():
    skus_path = sys.argv[1] if len(sys.argv) > 1 else "data/skus.json"
    reg = json.load(open(skus_path))
    skus = reg.get("skus", reg)
    print(f"# GTIN-in-payload probe — {len(skus)} SKUs in {skus_path}")
    print(f"# EBAY_APP_ID set: {bool(os.environ.get('EBAY_APP_ID'))}\n")
    summary = {}
    for slug, entry in skus.items():
        slug, verdict, detail = inspect(slug, entry)
        summary.setdefault(verdict.split()[0], []).append(slug)
        print(f"=== {slug} -> {verdict} ===")
        if detail:
            print(f"  brand/mpn: {detail.get('brand')!r} / {detail.get('mpn')!r}")
            if detail.get("code_aspects"):
                print(f"  CODE ASPECTS: {detail['code_aspects']}")
            if detail.get("product_codes"):
                print(f"  PRODUCT CODES: {detail['product_codes']}")
            print(f"  all aspect names ({len(detail['all_aspect_names'])}): "
                  f"{detail['all_aspect_names']}")
        print()
    print("## VERDICT BUCKETS")
    for v, slugs in sorted(summary.items()):
        print(f"  {v}: {len(slugs)} — {', '.join(slugs)}")
    print("\n## READ")
    n_primary = len(summary.get("GTIN-FOUND", []))
    n_aspect = len(summary.get("GTIN-FOUND-ASPECT", []))
    n_none = len(summary.get("NO-GTIN", []))
    if n_primary or n_aspect:
        print(f"  GTIN present in the payload we already fetch for "
              f"{n_primary + n_aspect}/{len(skus)} SKUs "
              f"({n_primary} authoritative product.gtins/additionalProductIdentities, "
              f"{n_aspect} aspect-only).")
        print("  Layer 1 confirmed: add the extraction to _extract_identity reading")
        print("  product.gtins / additionalProductIdentities (location shown per-SKU).")
        print("  Normalize UPC-12/EAN-13 -> GTIN-14 on the way in (substrate spec).")
        if n_none:
            print(f"  The {n_none} NO-GTIN SKUs are the spec's named gap (used / not")
            print("  catalog-associated) -> Wikidata-by-brand+mpn is the complement there.")
    else:
        print("  No GTIN in eBay payload for ANY of these SKUs. Either the catalog is")
        print("  used-goods-heavy (no seller catalog association) or these were minted")
        print("  from thin listings -> the identity key must come from Wikidata/GS1,")
        print("  queried by the brand+mpn we already hold. eBay aspects are NOT the")
        print("  identity-key source for this catalog.")

if __name__ == "__main__":
    main()
