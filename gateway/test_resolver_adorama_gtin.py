"""Visible contract for resolver_adorama_gtin (rung E — airlocked gateway cell).

Ported from the factory STUB test (authored 2026-08-04 as the executable
expectation) now that the feed has landed. The cell reads the PRE-NORMALISED
`adorama-search-index.json` shape {brand, gtin, image, model, mpn, price, url}
directly (no ingest.product_feed in the gateway airlock), so fixtures inject
normalised rows via `index=` rather than raw feed dicts via `feed_rows=`.

SourceResolution shape (spec §Cells): keys source, identity, confidence,
deterministic, aliases, relations, raw, why (+ buyable_url, rung E's CTA).
"""
from __future__ import annotations

from resolver_adorama_gtin import resolve

# A normalised index row (the shape the nightly writes).
PEAK_ROW = {
    "brand": "Peak Design", "model": "Peak Design Travel Tripod",
    "gtin": "0818373021234", "mpn": "TT-CB-5",
    "url": "https://adorama.prf.hn/click/camref:1101l5Pw9q/destination/pdtt",
    "image": "https://www.adorama.com/images/pdtt.jpg", "price": "599.95",
}


def test_no_feed_returns_none_stub_posture():
    # Empty/absent index -> None, so the orchestrator escalates to the floor.
    t = {"vendor": "Peak Design", "model": "Peak Design Travel Tripod"}
    assert resolve(t, index=None, index_path="/nonexistent/does-not-exist.json") is None
    assert resolve(t, index=[]) is None


def test_gtin_exact_match_is_deterministic():
    t = {"vendor": "Peak Design", "model": "Travel Tripod", "gtin": "0818373021234"}
    r = resolve(t, index=[PEAK_ROW])
    assert r is not None
    assert r["source"] == "adorama"
    assert r["deterministic"] is True
    assert r["confidence"] == 1.0
    assert r["identity"]["gtin"] == "0818373021234"
    assert r["identity"]["mpn"] == "TT-CB-5"
    assert r["identity"]["brand"] == "Peak Design"


def test_gtin_matches_across_length_normalisation():
    # A GTIN-12 target hits the GTIN-14 canon of the same product (and vice-versa).
    t = {"vendor": "Peak Design", "model": "Travel Tripod",
         "gtin": "818373021234"}  # 12-digit UPC of the row's 13-digit EAN
    r = resolve(t, index=[PEAK_ROW])
    assert r is not None and r["deterministic"] is True


def test_mpn_exact_is_deterministic_and_brand_scoped():
    # Same MPN + agreeing brand -> deterministic hit.
    t = {"vendor": "Peak Design", "model": "Travel Tripod", "mpn": "tt-cb-5"}
    r = resolve(t, index=[PEAK_ROW])
    assert r is not None and r["deterministic"] is True
    # Same MPN string but a DIFFERENT brand must NOT collide (cross-brand part no).
    t2 = {"vendor": "Canon", "model": "Whatever", "mpn": "tt-cb-5"}
    assert resolve(t2, index=[PEAK_ROW]) is None


def test_placeholder_mpn_is_never_a_join_key():
    # A seller placeholder ('Does Not Apply') carried on both sides must not merge.
    row = dict(PEAK_ROW, mpn="Does Not Apply", gtin="")
    t = {"vendor": "Sony", "model": "A7 IV", "mpn": "Does Not Apply"}
    assert resolve(t, index=[row]) is None


def test_brand_model_match_without_gtin_is_not_deterministic():
    t = {"vendor": "Peak Design", "model": "Peak Design Travel Tripod"}  # no join key
    r = resolve(t, index=[PEAK_ROW])
    assert r is not None
    assert r["deterministic"] is False
    assert r["confidence"] < 1.0
    assert r["identity"]["brand"] == "Peak Design"


def test_join_key_present_but_no_hit_returns_none_not_a_guess():
    # Target carries a GTIN that misses -> None (never fall to a brand+model guess).
    t = {"vendor": "Peak Design", "model": "Peak Design Travel Tripod",
         "gtin": "9999999999999"}
    assert resolve(t, index=[PEAK_ROW]) is None


def test_no_matching_row_returns_none():
    t = {"vendor": "Sony", "model": "A7S III", "gtin": "0000000000001"}
    assert resolve(t, index=[PEAK_ROW]) is None


def test_buyable_url_rides_along_as_the_cta():
    # Rung E's distinct value over C/D: the Partnerize-wrapped buyable link.
    t = {"vendor": "Peak Design", "model": "Travel Tripod", "gtin": "0818373021234"}
    r = resolve(t, index=[PEAK_ROW])
    assert r["buyable_url"] == PEAK_ROW["url"]


def test_relations_shape_always_present_and_empty():
    # No relations from this source; key present + well-shaped for uniform merge.
    t = {"vendor": "Peak Design", "model": "Travel Tripod", "gtin": "0818373021234"}
    r = resolve(t, index=[PEAK_ROW])
    assert r["relations"] == {"predecessor": [], "competitor": []}
