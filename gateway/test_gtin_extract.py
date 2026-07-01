"""
test_gtin_extract.py — verify GTIN extraction + normalization against the exact
payload shapes the live probe (probe_gtin_in_payload.py, 2026-07-01) observed.

Fixtures ARE the probe's real findings across the 14-SKU catalog, so these tests
lock the module to production reality, not an imagined schema:
  - authoritative product.gtins + additionalProductIdentities (sony-a7s-iii)
  - the canon-r5 CONFLICT (product.gtins disagrees with aspect UPC; 5 identifiers)
  - aspect-only UPC, no product container (the Sigmas)
  - NO-GTIN (used / not catalog-associated) -> gtin is None
"""
import gtin_extract as gx


# ---------------------------------------------------------------------------
# normalization + check digit
# ---------------------------------------------------------------------------

def test_upc12_pads_to_gtin14_valid():
    # Sigma 35 Art: bare UPC-12 from an aspect, valid GS1 check digit
    g14, valid = gx.normalize_to_gtin14("198983304656")
    assert g14 == "00198983304656"
    assert valid is True


def test_ean13_zero_padded_input_normalizes():
    # sony-a7s-iii: eBay hands a zero-padded 13-digit string
    g14, valid = gx.normalize_to_gtin14("0027242920569")
    assert g14 == "00027242920569"
    assert valid is True


def test_bare_ean13_canon_r5_product_gtin():
    # canon-r5 product.gtins[0] is a bare EAN-13 (a Canon JAN)
    g14, valid = gx.normalize_to_gtin14("4549292157345")
    assert g14 == "04549292157345"
    assert valid is True


def test_bad_check_digit_flagged_invalid():
    # last digit corrupted -> normalized but valid=False (kept in receipt, not join)
    g14, valid = gx.normalize_to_gtin14("0027242920560")
    assert g14 == "00027242920560"
    assert valid is False


def test_unparseable_width_returns_none():
    g14, valid = gx.normalize_to_gtin14("12345")   # 5 digits, not a GTIN width
    assert g14 is None and valid is False


def test_check_digit_known_vector():
    # UPC-A 036000291452 -> check digit 2 (classic GS1 worked example)
    assert gx.gtin14_check_digit("0000036000291045"[:13]) in range(10)
    body = "003600029145"          # 12-digit body region
    g14, valid = gx.normalize_to_gtin14(body + "2")
    assert valid is True


# ---------------------------------------------------------------------------
# extraction — authoritative container
# ---------------------------------------------------------------------------

def _sony_a7s_iii_raw():
    return {
        "product": {
            "gtins": ["0027242920569"],
            "additionalProductIdentities": [
                {"productIdentity": [{"identifierType": "UPC",
                                      "identifierValue": "0027242920569"}]},
                {"productIdentity": [{"identifierType": "EAN",
                                      "identifierValue": "0027242920569"}]},
            ],
        },
        "localizedAspects": [
            {"name": "UPC", "value": "0027242920569"},
            {"name": "Brand", "value": "Sony"},
        ],
    }


def test_authoritative_gtin_chosen_no_conflict():
    out = gx.extract_gtin(_sony_a7s_iii_raw())
    assert out["gtin"] == "00027242920569"
    prov = out["gtin_provenance"]
    assert prov["chosen_source"] == "product.gtins"
    assert prov["conflict"] is False
    # every code seen is retained (1 product.gtins + 2 additional + 1 aspect = 4
    # obs here; all normalize to the SAME gtin14, hence no conflict)
    assert len(prov["observations"]) == 4
    assert all(o["valid"] for o in prov["observations"])


# ---------------------------------------------------------------------------
# extraction — the canon-r5 CONFLICT (the reason we keep a fuller receipt)
# ---------------------------------------------------------------------------

def _canon_r5_raw():
    return {
        "product": {
            "gtins": ["4549292157345"],
            "additionalProductIdentities": [
                {"productIdentity": [{"identifierType": "UPC",
                                      "identifierValue": "0013803325812"}]},
                {"productIdentity": [{"identifierType": "UPC",
                                      "identifierValue": "0013803327809"}]},
                {"productIdentity": [{"identifierType": "EAN",
                                      "identifierValue": "0013803325812"}]},
                {"productIdentity": [{"identifierType": "EAN",
                                      "identifierValue": "0013803327809"}]},
                {"productIdentity": [{"identifierType": "EAN",
                                      "identifierValue": "4549292157345"}]},
            ],
        },
        "localizedAspects": [{"name": "UPC", "value": "0013803325812"}],
    }


def test_canon_r5_conflict_flagged():
    out = gx.extract_gtin(_canon_r5_raw())
    prov = out["gtin_provenance"]
    # canonical still chosen from the authoritative container (precedence holds)...
    assert out["gtin"] == "04549292157345"
    assert prov["chosen_source"] == "product.gtins"
    # ...but the disagreement is surfaced, NOT silently collapsed
    assert prov["conflict"] is True
    # all five identifiers + the aspect are retained for later adjudication
    raws = {o["raw"] for o in prov["observations"]}
    assert "4549292157345" in raws
    assert "0013803325812" in raws
    assert "0013803327809" in raws


def test_canon_r5_distinct_valid_codes_drive_conflict():
    out = gx.extract_gtin(_canon_r5_raw())
    valids = {o["gtin14"] for o in out["gtin_provenance"]["observations"]
              if o["valid"]}
    # three genuinely different products' codes ride this one ePID
    assert len(valids) >= 2


# ---------------------------------------------------------------------------
# extraction — aspect-only (the Sigmas: UPC in aspects, no product container)
# ---------------------------------------------------------------------------

def _sigma_aspect_only_raw():
    return {
        "product": {},   # no catalog association -> empty container
        "localizedAspects": [
            {"name": "Brand", "value": "Sigma"},
            {"name": "MPN", "value": "304965"},
            {"name": "UPC", "value": "198983304656"},
        ],
    }


def test_aspect_only_gtin_captured():
    out = gx.extract_gtin(_sigma_aspect_only_raw())
    assert out["gtin"] == "00198983304656"
    prov = out["gtin_provenance"]
    assert prov["chosen_source"] == "aspect"
    assert prov["conflict"] is False
    assert len(prov["observations"]) == 1


# ---------------------------------------------------------------------------
# extraction — NO-GTIN (used / not catalog-associated -> the Wikidata tail)
# ---------------------------------------------------------------------------

def _no_gtin_raw():
    return {
        "product": {},
        "localizedAspects": [
            {"name": "Brand", "value": "Sony"},
            {"name": "Model", "value": "ILCE-7RM5"},
            {"name": "Type", "value": "Mirrorless"},
        ],
    }


def test_no_gtin_returns_none():
    out = gx.extract_gtin(_no_gtin_raw())
    assert out["gtin"] is None
    assert out["gtin_provenance"]["chosen_source"] is None
    assert out["gtin_provenance"]["conflict"] is False
    assert out["gtin_provenance"]["observations"] == []


def test_empty_payload_safe():
    out = gx.extract_gtin({})
    assert out["gtin"] is None
    assert out["gtin_provenance"]["observations"] == []


def test_none_payload_safe():
    out = gx.extract_gtin(None)
    assert out["gtin"] is None


# ---------------------------------------------------------------------------
# precedence: aspect present alongside authoritative -> authoritative wins
# ---------------------------------------------------------------------------

def test_precedence_prefers_product_over_aspect_when_agree():
    raw = {
        "product": {"gtins": ["0719821437895"]},
        "localizedAspects": [{"name": "UPC", "value": "0719821437895"}],
    }
    out = gx.extract_gtin(raw)
    assert out["gtin"] == "00719821437895"
    assert out["gtin_provenance"]["chosen_source"] == "product.gtins"
    assert out["gtin_provenance"]["conflict"] is False
