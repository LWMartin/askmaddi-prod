"""Visible contract for resolver_mfr_surface (rung C — fresh-gear surface).

Authored 2026-08-04 as the executable expectation BEFORE the worker. The adapter
resolves identity from the manufacturer/Shopify page named by target['source_url'],
via an INJECTED fetch client (url -> html). It MUST reuse the existing
fact_pipeline.remix_metafield extractor for Hydrogen pages (extract_remix_context ->
find_root_product), and fall back to JSON-LD Product parsing for non-Hydrogen pages.

SourceResolution shape (spec §Cells): source, identity, confidence, deterministic,
aliases, relations, raw, why. This source fills relations empty (rung D fills them).
"""
from __future__ import annotations

import json

from resolver_mfr_surface import resolve


# ── fixtures (built inline; json.dumps guarantees the balanced-brace blob the
#    real extract_remix_context requires) ────────────────────────────────────
def _hydrogen_html(root_product: dict) -> str:
    ctx = {"state": {"loaderData": {
        "routes/($lang).products.$handle": {"rootProduct": root_product}}}}
    return ("<!doctype html><html><head><title>PD</title></head><body>"
            f"<script>window.__remixContext = {json.dumps(ctx)};</script>"
            "</body></html>")


PD_ROOT = {
    "title": "Travel Tripod",
    "vendor": "Peak Design",
    "handle": "travel-tripod",
    "featuredImage": {"url": "https://cdn.shopify.com/pd-travel-tripod.jpg"},
    "variants": {"nodes": [{"barcode": "0818373021234", "sku": "TT-CB-5"}]},
    "infoBlocks": {"references": {"nodes": [
        {"title": {"value": "Specs"},
         "textContent": {"value": "**Max Height**\n152 cm\n**Weight**\n1.56 kg"}}]}},
}

JSONLD_HTML = (
    "<!doctype html><html><head>"
    '<script type="application/ld+json">'
    + json.dumps({
        "@context": "https://schema.org", "@type": "Product",
        "name": "RF 24-70mm F2.8L IS USM", "brand": {"name": "Canon"},
        "gtin13": "4549292176100", "mpn": "3680C002",
        "image": "https://static.canon.com/rf2470.jpg",
    })
    + "</script></head><body>no remix context here</body></html>"
)


def test_no_source_url_returns_none_without_fetching():
    called = []
    r = resolve({"vendor": "Peak Design", "model": "Travel Tripod"},
                fetch=lambda url: called.append(url) or "")
    assert r is None
    assert called == []  # must not fetch when there is nothing to fetch


def test_hydrogen_extracts_identity_and_name_aliases():
    target = {"vendor": "Peak Design", "model": "Travel Tripod",
              "source_url": "https://peakdesign.com/products/travel-tripod"}
    r = resolve(target, fetch=lambda url: _hydrogen_html(PD_ROOT))
    assert r is not None
    assert r["source"] == "mfr_surface"
    assert r["identity"]["gtin"] == "0818373021234"
    assert r["identity"]["mpn"] == "TT-CB-5"
    assert r["identity"]["brand"] == "Peak Design"
    assert "Travel Tripod" in r["identity"]["canonical_model"]
    assert r["deterministic"] is True                      # gtin recovered
    # aliases are PRODUCT-NAME variants, never spec field names
    assert any("travel" in a.lower() for a in r["aliases"])
    assert "Max Height" not in r["aliases"]


def test_fetch_receives_the_source_url():
    seen = {}
    target = {"vendor": "Peak Design", "model": "Travel Tripod",
              "source_url": "https://peakdesign.com/products/travel-tripod"}
    resolve(target, fetch=lambda url: seen.setdefault("url", url) or _hydrogen_html(PD_ROOT))
    assert seen["url"] == target["source_url"]


def test_jsonld_fallback_when_not_hydrogen():
    target = {"vendor": "Canon", "model": "RF 24-70",
              "source_url": "https://canon.com/rf2470"}
    r = resolve(target, fetch=lambda url: JSONLD_HTML)
    assert r is not None
    assert r["source"] == "mfr_surface"
    assert r["identity"]["gtin"] == "4549292176100"
    assert r["identity"]["mpn"] == "3680C002"
    assert r["identity"]["brand"] == "Canon"
    assert r["deterministic"] is True


def test_unparseable_html_returns_none():
    target = {"vendor": "Nobody", "model": "Nothing",
              "source_url": "https://example.com/x"}
    assert resolve(target, fetch=lambda url: "<html><body>nope</body></html>") is None
