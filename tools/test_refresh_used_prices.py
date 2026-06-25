#!/usr/bin/env python3
"""Tests for tools/refresh_used_prices.py — run: python3 -m pytest tools/test_refresh_used_prices.py"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import price_sidecar
from refresh_used_prices import (
    compute_bands, condition_slug, listing_matches, significant_tokens,
    refresh_card, MIN_SAMPLE,
)


def _item(name, price, condition="Pre-owned", currency="USD"):
    return {"name": name, "price": str(price), "condition": condition,
            "currency": currency, "url": "https://www.ebay.com/itm/1"}


# ── condition mapping ────────────────────────────────────────────────────────
def test_condition_slugs():
    assert condition_slug("Open box") == "open_box"
    assert condition_slug("Excellent - Refurbished") == "refurbished"
    assert condition_slug("Pre-owned") == "pre_owned"
    assert condition_slug("Used") == "pre_owned"
    assert condition_slug("New") is None
    assert condition_slug("Brand New") is None
    assert condition_slug("For parts or not working") is None
    assert condition_slug("") is None


# ── title gating ─────────────────────────────────────────────────────────────
def test_blacklist_rejects_accessories():
    toks = significant_tokens("Sony A7 IV body")
    assert not listing_matches("Sony A7 IV battery 2-pack NP-FZ100", toks)
    assert not listing_matches("Body cap for Sony A7 IV", toks)
    assert not listing_matches("Sony A7 IV FOR PARTS cracked screen", toks)
    assert listing_matches("Sony Alpha A7 IV Mirrorless Camera", toks)


def test_token_gate_rejects_wrong_generation():
    toks = significant_tokens("Sony A7 IV body")
    assert not listing_matches("Sony Alpha A7 III Mirrorless Camera", toks)


def test_stopwords_not_required():
    assert "body" not in significant_tokens("Sony A7 IV body")
    assert "kit" not in significant_tokens("camera kit")
    # variant discriminators are NOT stopwords — they must bind (2026-06-10 fix)
    assert "carbon" in significant_tokens("Peak Design Travel Tripod carbon")


# ── band computation ─────────────────────────────────────────────────────────
def test_bands_min_per_condition_bucket():
    items = [
        _item("Sony A7 IV camera", 1500, "Pre-owned"),
        _item("Sony A7 IV camera", 1400, "Pre-owned"),
        _item("Sony A7 IV camera", 1650, "Open box"),
        _item("Sony A7 IV camera", 1550, "Excellent - Refurbished"),
    ]
    bands, n = compute_bands(items, "Sony A7 IV body")
    assert n == 4
    assert bands == {"pre_owned": 1400, "open_box": 1650, "refurbished": 1550}


def test_junk_floor_and_kit_ceiling_trim():
    items = [
        _item("Sony A7 IV camera", 1500, "Pre-owned"),
        _item("Sony A7 IV camera", 1450, "Pre-owned"),
        _item("Sony A7 IV camera", 1550, "Pre-owned"),
        _item("Sony A7 IV camera shutter unit", 90, "Pre-owned"),      # junk floor
        _item("Sony A7 IV camera 4 lens mega bundle", 5200, "Pre-owned"),  # kit ceiling
    ]
    bands, n = compute_bands(items, "Sony A7 IV body")
    assert n == 3
    assert bands["pre_owned"] == 1450  # the $90 part never becomes "from $90 used"


def test_sample_gate_returns_empty_bands():
    items = [_item("Sony A7 IV camera", 1500, "Pre-owned")] * (MIN_SAMPLE - 1)
    bands, n = compute_bands(items, "Sony A7 IV body")
    assert bands == {} and n == MIN_SAMPLE - 1


def test_new_listings_never_price_the_used_market():
    items = [_item("Sony A7 IV camera", 1998, "New")] * 10
    bands, _ = compute_bands(items, "Sony A7 IV body")
    assert bands == {}


def test_non_usd_excluded():
    items = [_item("Sony A7 IV camera", 1500, "Pre-owned", currency="EUR")] * 5
    bands, _ = compute_bands(items, "Sony A7 IV body")
    assert bands == {}


# ── card write + sold_last_90d honesty ───────────────────────────────────────
def test_refresh_card_writes_bands_to_sidecar_not_card(tmp_path):
    # Prices are captured state -> sidecar, NOT the tracked card JSON. The card
    # file must stay byte-clean (only used_query, authored); the bands land in
    # the gitignored sidecar keyed by card_id.
    card = {
        "card_id": "sony-a7iv",
        "identity": {"display_name": "Sony A7 IV"},
        "pricing": {"amazon_asin": "B09JZT6YK5", "used_query": "Sony A7 IV body"},
    }
    p = tmp_path / "sony-a7iv.json"
    p.write_text(json.dumps(card))
    sidecar = tmp_path / "used_prices.json"
    items = [_item("Sony A7 IV camera", v, "Pre-owned") for v in (1500, 1450, 1600)]

    assert refresh_card(p, items, sidecar_path=sidecar)

    # Card on disk is UNTOUCHED — no used_market written into the spine.
    on_disk = json.loads(p.read_text())
    assert "used_market" not in on_disk["pricing"]
    assert on_disk["pricing"]["amazon_asin"] == "B09JZT6YK5"  # untouched

    # Bands landed in the sidecar, keyed by card_id.
    um = price_sidecar.get_used_market("sony-a7iv", path=sidecar)
    assert um is not None
    assert um["bands"]["pre_owned"] == 1450
    assert um["sample_size"] == 3 and um["source"] == "ebay"
    assert "price_updated_at" in um
    assert "sold_last_90d" not in um  # Browse API = active asks; never fabricate sold comps


def test_refresh_card_gated_writes_nothing_to_sidecar(tmp_path):
    # Too few survivors -> gated -> sidecar gets no entry, so the card keeps its
    # honest fallback. Same semantics as the old "used_market untouched".
    card = {"card_id": "c", "identity": {"display_name": "Sony A7 IV"}, "pricing": {}}
    p = tmp_path / "c.json"
    p.write_text(json.dumps(card))
    sidecar = tmp_path / "used_prices.json"
    assert not refresh_card(p, [_item("Sony A7 IV camera", 1500)], sidecar_path=sidecar)
    assert price_sidecar.get_used_market("c", path=sidecar) is None
    # And the card is untouched on disk.
    assert "used_market" not in json.loads(p.read_text())["pricing"]


def test_overlay_populates_card_used_market_from_sidecar(tmp_path):
    # The build-side half of the contract: a static card with no prices gets its
    # used_market populated from the sidecar at overlay time, so every existing
    # renderer (used_cta, JSON-LD, bands table) reads it unchanged.
    sidecar = tmp_path / "used_prices.json"
    price_sidecar.set_used_market(
        "sony-a7iv",
        {"source": "ebay", "bands": {"pre_owned": 1450}, "sample_size": 3,
         "price_updated_at": "2026-06-25T00:00:00Z"},
        path=sidecar)
    card = {"card_id": "sony-a7iv", "identity": {"display_name": "Sony A7 IV"},
            "pricing": {"used_query": "Sony A7 IV body"}}

    price_sidecar.overlay(card, path=sidecar)

    assert card["pricing"]["used_market"]["bands"]["pre_owned"] == 1450
    assert card["pricing"]["used_query"] == "Sony A7 IV body"  # authored field survives

    from build_site import used_cta
    label, _url = used_cta(card)
    assert label == "from $1450 used"


def test_overlay_no_sidecar_entry_leaves_card_fallback(tmp_path):
    # A card the box has never priced: overlay is a no-op, card keeps its honest
    # "See used" fallback. No sidecar entry == never-refreshed.
    sidecar = tmp_path / "used_prices.json"
    card = {"card_id": "never-priced", "identity": {"display_name": "Mystery Cam"},
            "pricing": {"used_query": "Mystery Cam"}}
    price_sidecar.overlay(card, path=sidecar)
    assert "used_market" not in card["pricing"]
    from build_site import used_cta
    label, _url = used_cta(card)
    assert label == "See used"


# ── end-to-end: bands -> used_cta label ──────────────────────────────────────
def test_build_site_renders_from_price_label():
    from build_site import used_cta
    card = {
        "identity": {"display_name": "Sony A7 IV"},
        "pricing": {"used_market": {"source": "ebay",
                                    "bands": {"pre_owned": 1450, "open_box": 1650}}},
    }
    label, url = used_cta(card)
    assert label == "from $1450 used"
    assert "campid=5339138080" in url


def test_variant_token_binds_carbon_query_rejects_aluminum():
    toks = significant_tokens("Peak Design Travel Tripod carbon")
    assert "carbon" in toks
    assert not listing_matches("Peak Design Travel Tripod Aluminum", toks)
    assert listing_matches("Peak Design Travel Tripod Carbon Fiber TT-CB-5", toks)
