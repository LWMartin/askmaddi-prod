#!/usr/bin/env python3
"""
First-pass sufficiency diagnostic — is the second pass even necessary?

Before building a brand+mpn SEARCH (second pass), verify whether the GTIN is
recoverable from the tail listing's OWN payload (first pass) via a field or
reference we already fetch but don't fully exploit:

  Q1. Does L1 already read `product.additionalProductIdentities`, or only
      `product.gtins`? If a tail listing carries a GTIN in additional* that
      L1 skips, the "second pass" would be re-fetching data we already had.

  Q2. Does the NO-GTIN tail listing carry an `epid` (eBay catalog product
      reference) DESPITE having no GTIN? If yes, we can resolve that ePID's
      catalog record directly — a TARGETED lookup keyed by the product itself
      (no wrong-product risk), far cheaper and cleaner than a fuzzy brand+mpn
      search. This would fold the "second pass" back into a first-pass extend.

This decides architecture:
  - epid present on tail listings  -> extend first pass (resolve epid), search
    only as last resort.
  - epid absent, additional* empty -> product identity genuinely not on the
    listing; the brand+mpn search is irreducible.

Read-only. Dumps raw structure, draws no GTIN. Pure inspection.
"""
import json
import sys

sys.path.insert(0, "gateway")
import env_bootstrap  # noqa: E402
env_bootstrap.load_dotenv()

import ebay_api  # noqa: E402

sys.path.insert(0, "tools")
import probe_gtin_in_payload as l1  # noqa: E402


def _reconstruct_item_id(legacy):
    return "v1|" + str(legacy) + "|0"


def diagnose(slug, entry):
    legacy = entry.get("identity", {}).get("legacy_item_id")
    if not legacy:
        return {"slug": slug, "error": "no legacy_item_id"}
    try:
        r = ebay_api.resolve(_reconstruct_item_id(legacy))
    except Exception as e:
        return {"slug": slug, "error": str(e)}

    idn = r.get("identity", {})
    raw = r.get("_raw", {}) or {}
    product = raw.get("product", {}) or {}

    return {
        "slug": slug,
        "extracted_gtin": idn.get("gtin"),
        "extracted_epid": idn.get("epid"),
        # Q2: does the listing's OWN payload reference a catalog product?
        "raw_epid": product.get("epid") or raw.get("epid"),
        # Q1: is there a GTIN in additionalProductIdentities L1 might skip?
        "product_gtins": product.get("gtins"),
        "additional_identities": product.get("additionalProductIdentities"),
        # what product-level keys even exist, so we see the real shape
        "product_keys": sorted(product.keys()) if product else [],
    }


def main():
    skus_path = sys.argv[1] if len(sys.argv) > 1 else "data/skus.json"
    reg = json.load(open(skus_path))
    skus = reg.get("skus", reg)

    print("=" * 64)
    print("  FIRST-PASS SUFFICIENCY DIAGNOSTIC — is the 2nd pass necessary?")
    print("  read-only; inspects each NO-GTIN listing's own payload for an")
    print("  epid (catalog ref) or additionalProductIdentities we can exploit")
    print("=" * 64)

    no_gtin = []
    for slug, entry in skus.items():
        _s, verdict, _d = l1.inspect(slug, entry)
        if verdict.split()[0] == "NO-GTIN":
            no_gtin.append((slug, entry))
    print(f"\nNO-GTIN tail: {len(no_gtin)} SKU(s)\n")

    has_epid = []
    has_additional = []
    for slug, entry in no_gtin:
        d = diagnose(slug, entry)
        if d.get("error"):
            print(f"=== {slug} ===  ERROR {d['error']}\n")
            continue
        raw_epid = d["raw_epid"]
        addl = d["additional_identities"]
        pg = d["product_gtins"]
        print(f"=== {slug} ===")
        print(f"    extracted gtin:  {d['extracted_gtin']}")
        print(f"    epid (catalog ref on THIS listing): {raw_epid or '(none)'}")
        print(f"    product.gtins:                      {pg or '(none)'}")
        print(f"    additionalProductIdentities:        {addl or '(none)'}")
        print(f"    product-level keys present:         {d['product_keys']}")
        print()
        if raw_epid:
            has_epid.append(slug)
        if addl:
            has_additional.append(slug)

    print("-" * 64)
    print("FINDINGS:")
    print(f"  Tail SKUs whose OWN listing carries an epid (catalog ref): "
          f"{len(has_epid)}/{len(no_gtin)}  {', '.join(has_epid)}")
    print(f"  Tail SKUs with additionalProductIdentities L1 may skip:    "
          f"{len(has_additional)}/{len(no_gtin)}  {', '.join(has_additional)}")
    print("-" * 64)
    print("READ:")
    print("  - epid present  -> resolve epid directly (targeted, no wrong-")
    print("    product risk); fold 2nd pass into a first-pass extend.")
    print("  - additional* present -> L1 read gap; fix extraction, no search.")
    print("  - both absent   -> product identity genuinely off the listing;")
    print("    the brand+mpn search (2nd pass) is irreducible.")


if __name__ == "__main__":
    main()
