"""
gtin_extract.py — pull a canonical GTIN + a full provenance receipt from an
eBay resolve() payload.
================================================================================
Substrate spec step 5 (`maddi-product-substrate.md`): "surface GTIN from
ebay_api.resolve PRODUCT fieldgroup, normalize UPC/EAN -> GTIN-14." The live
`_extract_identity` reads brand/mpn from the same payload and discards every
product code. This module is the missing read.

WHAT THE PROBE FOUND (2026-07-01, 14 live SKUs, probe_gtin_in_payload.py):
  - 5 SKUs: authoritative `product.gtins` + `product.additionalProductIdentities`
  - 2 SKUs: UPC only in `localizedAspects` (no product container) — the Sigmas
  - 7 SKUs: no code at all (used / not catalog-associated) -> Wikidata tail
  - canon-r5: product.gtins (4549292157345 / a JAN-EAN) CONFLICTS with its
    aspect UPC (0013803325812) and lists FIVE identifiers under
    additionalProductIdentities — one ePID spanning regional/kit variants.

DESIGN (fuller-receipt, Lee 2026-07-01): the canonical `gtin` is a single
GTIN-14 chosen by an explicit precedence rule, for the join/encoding axis. A
`gtin_provenance` receipt sits beside it holding EVERY code seen — source path,
raw + normalized form, check-digit validity, and a `conflict` flag when the
authoritative container disagrees with the other observations. Tiny footprint,
never reconstructable later, so we keep it.

CORRECTNESS NOTE the data forces: eBay validates GTIN *format* but not
GTIN<->product *match* (substrate spec, Axis A failure mode). So "prefer
product.gtins" is a precedence rule, NOT a correctness guarantee. When
product.gtins conflicts with the other observations we DO NOT silently bless
one — we set conflict=True so the caller can route to /admin (abstain->human,
the spec's non-negotiable discipline). This module only reports the conflict;
it does not decide review (that stays with the caller / registry writer).

Pure functions only. No network, no ebay_api import, no I/O. Fully unit-testable
against synthetic payloads; the probe's real payloads are the fixtures.
"""

# Precedence order for choosing the single canonical GTIN. Authoritative product
# container first, then structured additionalProductIdentities, then a code that
# rode localizedAspects. Documented so the choice is auditable, not implicit.
SOURCE_PRECEDENCE = (
    "product.gtins",
    "product.additionalProductIdentities",
    "aspect",
)


def _digits(raw):
    """Strip to digits only. eBay values arrive as bare or zero-padded strings."""
    return "".join(ch for ch in str(raw) if ch.isdigit())


def gtin14_check_digit(digits14_body):
    """GS1 mod-10 check digit for the first 13 digits of a GTIN-14.

    GS1 weighting runs right-to-left over the 13 body digits as 3,1,3,1,...
    (the rightmost body digit gets weight 3). The check digit makes the total
    a multiple of 10. Works for any GTIN width once left-padded to 14, because
    leading zeros contribute nothing to the sum.
    """
    if len(digits14_body) != 13 or not digits14_body.isdigit():
        raise ValueError("check-digit body must be exactly 13 digits")
    total = 0
    # rightmost body digit -> weight 3; alternate 3,1 moving left
    for i, ch in enumerate(reversed(digits14_body)):
        total += int(ch) * (3 if i % 2 == 0 else 1)
    return (10 - (total % 10)) % 10


def normalize_to_gtin14(raw):
    """Normalize a UPC-12 / EAN-13 / GTIN-14 (or zero-padded variant) to GTIN-14.

    Returns (gtin14, valid) where valid is True iff the trailing check digit is
    consistent with the leading 13 under GS1 mod-10. A malformed code returns
    (padded_or_None, False) so the caller can keep it in provenance but never
    treat it as a trusted join key.

    Widths accepted: 8 (EAN-8), 12 (UPC-A), 13 (EAN-13), 14 (GTIN-14). Anything
    else -> (None, False): unparseable, receipt-only. (Photography vertical is
    UPC-12/EAN-13 dominant; EAN-8 folds cleanly, ISBN-13 would too if it appeared.)
    """
    d = _digits(raw)
    if len(d) not in (8, 12, 13, 14):
        return None, False
    padded = d.rjust(14, "0")
    valid = int(padded[-1]) == gtin14_check_digit(padded[:13])
    return padded, valid


def _collect_from_additional(api_list):
    """Flatten additionalProductIdentities into [(identifierType, value), ...].

    Shape (verified against live payloads): a list of
      {'productIdentity': [{'identifierType': 'UPC', 'identifierValue': '...'}, ...]}
    canon-r5 nests five of these; most SKUs nest two (a UPC and an EAN of the
    same value). Defensive against missing keys / non-list shapes.
    """
    out = []
    for block in api_list or []:
        for pid in (block or {}).get("productIdentity", []) or []:
            itype = (pid or {}).get("identifierType", "")
            ival = (pid or {}).get("identifierValue", "")
            if ival:
                out.append((itype, ival))
    return out


def extract_gtin(raw):
    """Extract canonical GTIN + provenance receipt from a resolve() `_raw` payload.

    `raw` is the payload's `_raw` dict (what the probe read): may hold `product`
    and/or `localizedAspects`. Returns:

        {
          "gtin": "00027242920569" | None,     # canonical GTIN-14 join key
          "gtin_provenance": {
            "chosen_source": "product.gtins" | ... | None,
            "conflict": bool,                    # authoritative vs others disagree
            "observations": [                    # EVERY code seen, in discovery order
              {"source": "product.gtins", "identifier_type": "GTIN",
               "raw": "0027242920569", "gtin14": "00027242920569", "valid": true},
              ...
            ]
          }
        }

    Pure: no side effects, no review decision. The caller (registry writer) reads
    `conflict` / `gtin is None` to set needs_review, per the substrate spec.
    """
    product = (raw or {}).get("product", {}) or {}
    aspects = (raw or {}).get("localizedAspects", []) or []

    observations = []

    # 1. product.gtins — authoritative dedicated array
    for val in product.get("gtins", []) or []:
        g14, valid = normalize_to_gtin14(val)
        observations.append({
            "source": "product.gtins", "identifier_type": "GTIN",
            "raw": str(val), "gtin14": g14, "valid": valid,
        })

    # 2. product.additionalProductIdentities — structured {type,value} identifiers
    for itype, val in _collect_from_additional(
            product.get("additionalProductIdentities")):
        g14, valid = normalize_to_gtin14(val)
        observations.append({
            "source": "product.additionalProductIdentities",
            "identifier_type": itype or "UNKNOWN",
            "raw": str(val), "gtin14": g14, "valid": valid,
        })

    # 3. localizedAspects — a UPC/EAN/GTIN aspect (the Sigmas' only code)
    for asp in aspects:
        name = (asp.get("name") or "")
        if name.upper() in ("UPC", "EAN", "GTIN", "ISBN"):
            val = asp.get("value") or ""
            if val:
                g14, valid = normalize_to_gtin14(val)
                observations.append({
                    "source": "aspect", "identifier_type": name.upper(),
                    "raw": str(val), "gtin14": g14, "valid": valid,
                })

    # ---- choose the canonical gtin by precedence, among VALID observations ----
    chosen, chosen_source = None, None
    for src in SOURCE_PRECEDENCE:
        for obs in observations:
            if obs["source"] == src and obs["valid"]:
                chosen, chosen_source = obs["gtin14"], src
                break
        if chosen:
            break

    # ---- conflict: do the distinct VALID normalized codes disagree? ----
    distinct_valid = {o["gtin14"] for o in observations if o["valid"]}
    conflict = len(distinct_valid) > 1

    return {
        "gtin": chosen,
        "gtin_provenance": {
            "chosen_source": chosen_source,
            "conflict": conflict,
            "observations": observations,
        },
    }
