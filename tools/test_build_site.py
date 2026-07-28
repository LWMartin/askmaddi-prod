"""Tests for the teaser axis role-selection design (2026-06-03).

Design rules under test:
  1. Three roles: most_discussed (volume), highest_rated (best pos-ratio),
     biggest_gripe (worst pos-ratio).
  2. Volume floor: total >= max(15, 0.1 * top axis volume) — keeps noise
     axes out of the high/low slots.
  3. Meta-axes (generation_context, price) excluded from high/low slots,
     eligible for most_discussed.
  4. Collisions take the next distinct axis; sparse cards fall back to
     volume order with role=None.

Run from repo root:  python -m pytest tools/test_build_site.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_site import (  # noqa: E402
    select_teaser_axes, teaser_entry,
    TEASER_ROLE_MOST, TEASER_ROLE_HIGH, TEASER_ROLE_LOW,
)


def _axis(axis_id, pos, neg, neu=0, display=None):
    total = pos + neg + neu
    return {
        "axis_id": axis_id,
        "display_name": display or axis_id.replace("_", " ").title(),
        "sentiment": {"pos": pos, "neg": neg, "neu": neu, "total": total},
    }


def _card(lead, detail=None, card_id="test-sku"):
    return {
        "card_id": card_id,
        "identity": {"display_name": "Test", "brand": "T",
                     "category": "x", "subcategory": "y"},
        "lead_axes": lead,
        "detail_axes": detail or [],
        "sources": [],
    }


def _roles(picks):
    return {role: a["axis_id"] for a, role in picks if role}


def test_three_roles_assigned():
    card = _card([
        _axis("video", 85, 150),     # volume 235 -> most discussed
        _axis("autofocus", 64, 38),  # 63% -> highest rated
        _axis("buffer", 42, 140),    # 23% -> biggest gripe
        _axis("handling", 52, 49),
    ])
    r = _roles(select_teaser_axes(card))
    assert r[TEASER_ROLE_MOST] == "video"
    assert r[TEASER_ROLE_HIGH] == "autofocus"
    assert r[TEASER_ROLE_LOW] == "buffer"


def test_volume_floor_excludes_noise_axes():
    """A tiny all-negative axis must not headline biggest_gripe, and a tiny
    all-positive axis must not headline highest_rated."""
    card = _card([
        _axis("video", 100, 142),    # 242 total -> floor = 24.2
        _axis("autofocus", 64, 38),
        _axis("buffer", 42, 140),
        _axis("weight", 0, 4),       # 4 < floor: 0% but ineligible
        _axis("build", 20, 2),       # 22 < floor: 91% but ineligible
    ])
    r = _roles(select_teaser_axes(card))
    assert r[TEASER_ROLE_LOW] == "buffer"      # not weight
    assert r[TEASER_ROLE_HIGH] == "autofocus"  # not build


def test_meta_axes_excluded_from_high_low_but_allowed_as_most_discussed():
    card = _card([
        _axis("generation_context", 90, 10),  # 90%: would win HIGH if eligible
        _axis("price", 2, 78),                # 2.5%: would win LOW if eligible
        _axis("autofocus", 60, 40),
        _axis("buffer", 30, 70),
    ])
    r = _roles(select_teaser_axes(card))
    assert r[TEASER_ROLE_MOST] == "generation_context"  # volume winner, eligible here
    assert r[TEASER_ROLE_HIGH] == "autofocus"
    assert r[TEASER_ROLE_LOW] == "buffer"


def test_collision_takes_next_distinct_axis():
    """When the most-discussed axis is also the worst-rated, the gripe slot
    takes the next-worst distinct axis rather than repeating."""
    card = _card([
        _axis("video", 20, 180),     # most discussed AND worst (10%)
        _axis("autofocus", 64, 38),
        _axis("buffer", 42, 100),    # 30% -> next-worst distinct
    ])
    picks = select_teaser_axes(card)
    ids = [a["axis_id"] for a, _ in picks]
    assert len(ids) == len(set(ids)), "an axis filled two slots"
    r = _roles(picks)
    assert r[TEASER_ROLE_MOST] == "video"
    assert r[TEASER_ROLE_LOW] == "buffer"


def test_sparse_card_falls_back_to_volume_order_unlabeled():
    """One qualifying axis + tiny axes: remaining slots fill by volume with
    role=None so the renderer shows no label on them."""
    card = _card([
        _axis("video", 100, 100),  # 200 -> floor 20; only qualifier
        _axis("weight", 1, 5),     # 6
        _axis("build", 3, 1),      # 4
    ])
    picks = select_teaser_axes(card)
    assert len(picks) == 3
    assert picks[0][1] == TEASER_ROLE_MOST
    assert picks[1] == (card["lead_axes"][1], None)  # weight, unlabeled
    assert picks[2] == (card["lead_axes"][2], None)  # build, unlabeled


def test_detail_axes_scanned_too():
    """High/low candidates come from the full card, not just lead_axes."""
    card = _card(
        lead=[_axis("video", 85, 150), _axis("handling", 52, 49)],
        detail=[_axis("evf", 45, 10)],  # 82% in detail_axes -> highest rated
    )
    r = _roles(select_teaser_axes(card))
    assert r[TEASER_ROLE_HIGH] == "evf"


def test_teaser_entry_carries_role_field():
    card = _card([
        _axis("video", 85, 150),
        _axis("autofocus", 64, 38),
        _axis("buffer", 42, 140),
    ])
    entry = teaser_entry(card)
    roles = [a["role"] for a in entry["top_axes"]]
    assert roles == [TEASER_ROLE_MOST, TEASER_ROLE_HIGH, TEASER_ROLE_LOW]
    # Shape stays backward compatible: axis/pos/neg/total all present.
    for a in entry["top_axes"]:
        for k in ("axis", "pos", "neg", "total"):
            assert k in a


def test_teaser_entry_carries_amazon_rung_url():
    card = _card([_axis("video", 85, 150)])
    card.setdefault("pricing", {})["amazon_asin"] = "B09JZT6YK5"
    entry = teaser_entry(card)
    assert "/dp/B09JZT6YK5" in entry["pricing"]["amazon_url"]
    assert "tag=askmaddi20-20" in entry["pricing"]["amazon_url"]


def test_teaser_entry_amazon_url_empty_for_absent_card():
    # Empty string, not a dead '#' link — cards.js omits the button entirely.
    card = _card([_axis("video", 85, 150)])
    card.setdefault("pricing", {})["amazon_absent"] = True
    assert teaser_entry(card)["pricing"]["amazon_url"] == ""


def test_teaser_entry_pricing_keeps_prior_keys():
    # Regression guard: adding amazon_url must not disturb the shape cards.js
    # already reads (new_price / new_url / used_price / used_url).
    entry = teaser_entry(_card([_axis("video", 85, 150)]))
    for k in ("new_price", "new_url", "used_price", "used_url", "amazon_url"):
        assert k in entry["pricing"]


def test_empty_card_yields_no_axes():
    assert select_teaser_axes(_card([])) == []


# ─── Affiliate tag enforcement (revenue regression: untagged /dp/ CTAs) ─────
from build_site import ensure_affiliate_tag, new_cta, used_cta, amazon_cta


def test_amazon_url_tagged_after_associates_reinstatement():
    # Associates reinstated 2026-07-27 with a NEW tracking id. Tagging a link
    # is always permitted; only DISPLAYING Amazon data is API-gated.
    out = ensure_affiliate_tag("https://www.amazon.com/dp/B09JZT6XXX")
    assert "tag=askmaddi20-20" in out


def test_dead_amazon_tag_never_reappears():
    # askmaddi-20 died with the suspension. It is NOT a substring of
    # askmaddi20-20, so a naive grep will not catch a regression — assert it.
    out = ensure_affiliate_tag("https://www.amazon.com/dp/B09JZT6XXX?tag=askmaddi-20")
    assert "askmaddi-20" not in out and "tag=askmaddi20-20" in out


def test_amazon_cta_carries_no_price_ever():
    # THE compliance invariant: without Creators API credentials we may link to
    # Amazon but may not display its price, rating, or review count. A card
    # carrying a price must still yield a bare, price-free Amazon label.
    card = {
        "identity": {"display_name": "Sony A7 IV"},
        "pricing": {"amazon_asin": "B09JZT6YK5", "current_new_usd": 2498, "msrp_usd": 2498},
    }
    label, url = amazon_cta(card)
    assert label == "See price on Amazon"
    assert "$" not in label and "2498" not in label
    assert url.startswith("https://www.amazon.com/dp/B09JZT6YK5")
    assert "tag=askmaddi20-20" in url


def test_amazon_cta_deep_links_asin_over_search():
    card = {"identity": {"display_name": "Sony A7 IV"},
            "pricing": {"amazon_asin": "B09JZT6YK5"}}
    _, url = amazon_cta(card)
    assert "/dp/B09JZT6YK5" in url and "/s?k=" not in url


def test_amazon_cta_falls_back_to_scoped_search_without_asin():
    card = {"identity": {"display_name": "Sony A7 IV"}, "pricing": {}}
    _, url = amazon_cta(card)
    assert "amazon.com/s?k=Sony+A7+IV" in url and "tag=askmaddi20-20" in url


def test_amazon_cta_suppressed_for_absent_card():
    # The e930bea wrong-product trap: a card VERIFIED not sold on Amazon gets
    # NO rung at all, rather than a close-match page for a different product.
    card = {"identity": {"display_name": "Peak Design Pro Tripod"},
            "pricing": {"amazon_absent": True}}
    assert amazon_cta(card) is None


def test_raw_ebay_item_url_gets_campaign_params():
    out = ensure_affiliate_tag("https://www.ebay.com/itm/123456789")
    assert "campid=5339138080" in out and "mkrid=711-53200-19255-0" in out


def test_non_program_domain_passthrough():
    url = "https://www.bhphotovideo.com/c/product/123"
    assert ensure_affiliate_tag(url) == url


def test_new_cta_wraps_adorama_search_by_default():
    # No explicit affiliate link, no exact product URL → product-scoped Adorama
    # search, Partnerize-wrapped. No Amazon anywhere.
    card = {"identity": {"display_name": "Sony A7 IV"}, "pricing": {}}
    label, url = new_cta(card)
    assert url.startswith("https://adorama.prf.hn/click/camref:1101l5Pw9q/destination:")
    assert "adorama.com/l/?searchinfo=Sony+A7+IV" in url
    assert "amazon" not in url.lower() and "tag=askmaddi-20" not in url
    assert label == "Check price at Adorama"


def test_used_cta_tags_raw_search_url():
    card = {
        "identity": {"display_name": "Sony A7 IV"},
        "pricing": {
            "used_market": {
                "affiliate_url": None,
                "search_url": "https://www.ebay.com/sch/i.html?_nkw=sony+a7+iv",
            }
        },
    }
    _, url = used_cta(card)
    assert "campid=5339138080" in url


def test_new_cta_prefers_exact_adorama_product_url_over_search():
    # When the feed/registry supplies an exact Adorama product URL it beats the
    # search, still Partnerize-wrapped.
    card = {
        "identity": {"display_name": "Sony A7 IV"},
        "pricing": {"adorama_url": "https://www.adorama.com/isoa7iv.html"},
    }
    _, url = new_cta(card)
    assert url == ("https://adorama.prf.hn/click/camref:1101l5Pw9q/"
                   "destination:https://www.adorama.com/isoa7iv.html")
    assert "searchinfo" not in url


def test_new_cta_honours_prewrapped_partnerize_affiliate_url():
    # An explicit prf.hn link (e.g. from the feed) is used verbatim, never
    # double-wrapped.
    card = {"identity": {"display_name": "X"},
            "pricing": {"affiliate_url": "https://prf.hn/l/abc123"}}
    _, url = new_cta(card)
    assert url == "https://prf.hn/l/abc123"


def test_new_cta_never_emits_amazon_even_with_asin_gtin():
    # Amazon ASIN/GTIN data may linger in the card; the new CTA must never link
    # to a dead-tag Amazon URL.
    card = {"identity": {"display_name": "Ulanzi F38 Zero"},
            "pricing": {"gtin": "00719821437895", "amazon_asin": "B09JZT6YK5"}}
    _, url = new_cta(card)
    assert "amazon" not in url.lower()
    assert "adorama.prf.hn" in url


def test_new_cta_label_shows_price_when_known():
    card = {"identity": {"display_name": "Sony A7 IV"},
            "pricing": {"current_new_usd": 2499, "adorama_url":
                        "https://www.adorama.com/isoa7iv.html"}}
    label, _ = new_cta(card)
    assert label == "$2499 new"


def test_partnerize_wrap_idempotent_and_scoped():
    from build_site import partnerize_wrap, PARTNERIZE_SHORT
    assert partnerize_wrap("https://prf.hn/l/x") == "https://prf.hn/l/x"  # already wrapped
    # a non-Adorama destination is never mis-attributed to Adorama
    assert partnerize_wrap("https://www.bhphotovideo.com/x") == "https://www.bhphotovideo.com/x"
    assert partnerize_wrap("") == PARTNERIZE_SHORT  # never a dead CTA


def test_apply_spine_gtins_merges_from_spine(tmp_path, monkeypatch):
    import build_site as bs
    spine = tmp_path / "skus.json"
    spine.write_text('{"skus": {"ulanzi-f38-zero": {"gtin": "00719821437895"}}}')
    monkeypatch.setattr(bs, "SKUS_SPINE_PATH", spine)
    cards = [{"card_id": "ulanzi-f38-zero", "pricing": {}},
             {"card_id": "unknown-card", "pricing": {}}]
    bs.apply_spine_gtins(cards)
    assert cards[0]["pricing"]["gtin"] == "00719821437895"
    assert "gtin" not in cards[1]["pricing"]


def test_new_cta_search_is_product_scoped_not_generic_homepage():
    # The default Adorama CTA must be scoped to THIS product (search), never the
    # bare homepage short link — a generic landing kills conversion.
    from build_site import PARTNERIZE_SHORT
    card = {"identity": {"display_name": "Canon R5"}, "pricing": {}}
    _, url = new_cta(card)
    assert url != PARTNERIZE_SHORT
    assert "searchinfo=Canon+R5" in url


# ─── SEO/OG batch (2026-06-10): head meta, JSON-LD, as-of, sitemap ──────────
import json as _json
import re as _re

from build_site import (  # noqa: E402
    render_page, schema_org_jsonld, write_sitemap, card_lastmod,
    used_price_asof, fmt_date_human, abs_url, BASE_URL,
)


def _seo_card(card_id="test-cam", bands=None, price_updated_at=None,
          image=None, last_built="2026-06-04T23:47:35+00:00"):
    used = {"source": "ebay"}
    if bands is not None:
        used["bands"] = bands
        used["sample_size"] = 5
    if price_updated_at:
        used["price_updated_at"] = price_updated_at
    ident = {"display_name": "Test Cam X1", "brand": "TestBrand",
             "category": "camera", "subcategory": "mirrorless"}
    if image:
        ident["image_hero"] = image
    return {
        "card_id": card_id,
        "identity": ident,
        "pricing": {"used_market": used},
        "freshness": {"last_built": last_built, "source_count": 7},
        "confidence": {"overall": "medium"},
        "synthesis": {"consensus_paragraph": "Reviewers broadly like it."},
        "sources": [], "lead_axes": [], "detail_axes": [],
    }


def test_head_carries_canonical_og_url_twitter():
    page = render_page(_seo_card(bands={"pre_owned": 400.0},
                             price_updated_at="2026-06-10T15:00:00+00:00"))
    assert f'<link rel="canonical" href="{BASE_URL}/cards/test-cam/">' in page
    assert f'<meta property="og:url" content="{BASE_URL}/cards/test-cam/">' in page
    assert '<meta property="og:site_name" content="AskMaddi">' in page
    assert '<meta name="twitter:card" content="' in page


def test_og_image_absolute_when_card_has_image():
    page = render_page(_seo_card(image="https://cdn.example.com/x1.jpg"))
    assert '<meta property="og:image" content="https://cdn.example.com/x1.jpg">' in page
    assert 'twitter:card" content="summary_large_image"' in page


def test_jsonld_product_with_used_aggregate_offer():
    card = _seo_card(bands={"pre_owned": 400.0, "open_box": 500.0},
                 price_updated_at="2026-06-10T15:00:00+00:00")
    page = render_page(card)
    m = _re.search(r'<script type="application/ld\+json">\n(.*?)\n  </script>',
                   page, _re.S)
    assert m, "JSON-LD block missing"
    obj = _json.loads(m.group(1).replace("<\\/", "</"))
    assert obj["@type"] == "Product"
    assert obj["brand"]["name"] == "TestBrand"
    offer = obj["offers"]
    assert offer["@type"] == "AggregateOffer"
    assert offer["lowPrice"] == "400.00" and offer["highPrice"] == "500.00"
    assert offer["itemCondition"].endswith("UsedCondition")
    assert offer["offerCount"] == 5
    assert "aggregateRating" not in obj  # honesty: no invented star scale


def test_jsonld_no_offer_when_bands_empty():
    obj = _json.loads(schema_org_jsonld(_seo_card(bands={}), "u", None, "d")
                      .replace("<\\/", "</"))
    assert "offers" not in obj  # Sigma-style gating carries into markup


def test_asof_renders_only_with_band_and_date():
    with_both = render_page(_seo_card(bands={"pre_owned": 400.0},
                                  price_updated_at="2026-06-10T15:50:21+00:00"))
    assert "Used price as of Jun 10, 2026" in with_both
    no_date = render_page(_seo_card(bands={"pre_owned": 400.0}))
    assert "Used price as of" not in no_date  # build time is not a price observation
    no_band = render_page(_seo_card(bands={}, price_updated_at="2026-06-10T15:00:00+00:00"))
    assert "Used price as of" not in no_band


def test_used_market_section_asof_line():
    page = render_page(_seo_card(bands={"pre_owned": 400.0},
                             price_updated_at="2026-06-10T15:50:21+00:00"))
    assert "Prices as of Jun 10, 2026" in page


def test_card_lastmod_prefers_newest_observation():
    c = _seo_card(bands={"pre_owned": 400.0},
              price_updated_at="2026-06-10T15:50:21+00:00",
              last_built="2026-06-04T23:47:35+00:00")
    assert card_lastmod(c) == "2026-06-10"
    assert card_lastmod(_seo_card()) == "2026-06-04"


def test_sitemap_contains_static_and_cards(tmp_path):
    cards = [_seo_card("alpha", bands={"pre_owned": 100.0},
                   price_updated_at="2026-06-10T00:00:01+00:00"),
             _seo_card("beta")]
    p = write_sitemap(tmp_path, cards)
    xml = p.read_text()
    assert f"<loc>{BASE_URL}/</loc>" in xml
    for s in ("/mission.html", "/privacy.html", "/terms.html"):
        assert f"<loc>{BASE_URL}{s}</loc>" in xml
    assert f"<loc>{BASE_URL}/cards/alpha/</loc>" in xml
    assert f"<loc>{BASE_URL}/cards/beta/</loc>" in xml
    assert "<lastmod>2026-06-10</lastmod>" in xml  # alpha price date wins; homepage too


def test_fmt_date_and_abs_url_edges():
    assert fmt_date_human("") == "" and fmt_date_human("garbage") == ""
    assert abs_url("images/logo.png") == f"{BASE_URL}/images/logo.png"
    assert abs_url("https://x.com/a.jpg") == "https://x.com/a.jpg"
    assert used_price_asof({"pricing": {}}) == ""


# ─── Clobber guard: --card refuses --manifest/--sitemap (2026-07-15) ─────────
# Whole-file outputs (manifest, sitemap) regenerate from only the cards loaded
# in one run; composed with --card that replaces the live grid/sitemap with a
# single entry (bitten live 2026-07-03). Guard fails loud at argparse level.
import json as _json
import subprocess as _subprocess

_BUILD_SITE = str(Path(__file__).parent / "build_site.py")


def _run_cli(*argv):
    return _subprocess.run([sys.executable, _BUILD_SITE, *argv],
                           capture_output=True, text=True)


def _write_min_card(tmp_path, card_id="guard-sku"):
    card = _card([_axis("video", 85, 150)], card_id=card_id)
    card["synthesis"] = {"summary": "x"}
    p = tmp_path / f"{card_id}.json"
    p.write_text(_json.dumps(card), encoding="utf-8")
    return p


def test_card_plus_manifest_refused(tmp_path):
    card = _write_min_card(tmp_path)
    r = _run_cli("--card", str(card), "--manifest",
                 "--output-dir", str(tmp_path / "out"))
    assert r.returncode == 2                      # argparse error, loud
    assert "whole" in r.stderr.lower()
    assert "--cards-dir" in r.stderr              # the recipe is in the message
    assert not (tmp_path / "out" / "cards-manifest.json").exists()


def test_card_plus_sitemap_refused(tmp_path):
    card = _write_min_card(tmp_path)
    r = _run_cli("--card", str(card), "--sitemap",
                 "--output-dir", str(tmp_path / "out"))
    assert r.returncode == 2
    assert not (tmp_path / "out" / "sitemap.xml").exists()


def test_card_alone_still_builds_detail_page(tmp_path):
    # The guard must not break the legitimate single-card detail rebuild.
    card = _write_min_card(tmp_path)
    r = _run_cli("--card", str(card), "--output-dir", str(tmp_path / "out"))
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "out" / "cards" / "guard-sku" / "index.html").exists()
    assert not (tmp_path / "out" / "cards-manifest.json").exists()


def test_cards_dir_with_manifest_and_sitemap_still_allowed(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_min_card(corpus, "sku-a")
    _write_min_card(corpus, "sku-b")
    r = _run_cli("--cards-dir", str(corpus), "--manifest", "--sitemap",
                 "--output-dir", str(tmp_path / "out"))
    assert r.returncode == 0, r.stderr
    manifest = _json.loads((tmp_path / "out" / "cards-manifest.json").read_text())
    assert len(manifest["cards"]) == 2            # full corpus, no shrinkage
    assert (tmp_path / "out" / "sitemap.xml").exists()


def test_new_cta_ignores_gtin_and_asin_routes_to_adorama():
    """Post-Amazon: GTIN no longer drives an Amazon search rung — the new CTA
    is Adorama, product-scoped by display name."""
    card = {
        "identity": {"display_name": "Ulanzi F38 Zero"},
        "pricing": {"gtin": "00719821437895"},
    }
    _, url = new_cta(card)
    assert "adorama.com/l/?searchinfo=Ulanzi+F38+Zero" in url
    assert "amazon" not in url.lower()


def test_new_cta_does_not_hijack_an_ebay_affiliate_url_for_new():
    """An eBay affiliate_url is a USED-market link (used_cta's domain). It must
    NOT become the 'buy new' CTA — only a pre-wrapped prf.hn link is honoured
    there; anything else falls to the Adorama destination. amazon_absent is now
    irrelevant (no Amazon rungs exist)."""
    card = {
        "identity": {"display_name": "Peak Design Pro Tripod"},
        "pricing": {"gtin": "00840262600000", "amazon_absent": True,
                    "affiliate_url": "https://www.ebay.com/itm/999"},
    }
    _, url = new_cta(card)
    assert "amazon.com" not in url
    assert "ebay.com/itm/999" not in url  # eBay link is not the NEW CTA
    assert "adorama.prf.hn" in url


def test_apply_asin_registry_marks_absent(tmp_path, monkeypatch):
    import build_site as bs
    reg = tmp_path / "asin_registry.json"
    reg.write_text('{"asins": {}, "absent": ["peak-design-pro-tripod"]}')
    monkeypatch.setattr(bs, "ASIN_REGISTRY_PATH", reg)
    cards = [{"card_id": "peak-design-pro-tripod", "pricing": {}},
             {"card_id": "sony-a7iv", "pricing": {}}]
    bs.apply_asin_registry(cards)
    assert cards[0]["pricing"].get("amazon_absent") is True
    assert "amazon_absent" not in cards[1]["pricing"]


# ── The role names are a cross-file contract (2026-07-28) ────────────────

def test_the_low_role_is_named_for_what_it_computes():
    """Pins the WIRE value, not the constant. The suite referenced
    TEASER_ROLE_LOW everywhere, so renaming "biggest_gripe" -> "lowest_rated"
    passed 758 tests without one of them noticing -- which is also how the
    original mislabel survived. The string ships in cards-manifest.json and is
    read by browser/js/cards.js, so it is a contract, not an internal name.

    "biggest gripe" claimed criticism while the selector ranked positive
    share. sony-a7s-iii tagged mount compatibility as the biggest gripe on
    THREE negatives out of 67.
    """
    from build_site import TEASER_ROLE_MOST, TEASER_ROLE_HIGH, TEASER_ROLE_LOW
    assert TEASER_ROLE_MOST == "most_discussed"
    assert TEASER_ROLE_HIGH == "highest_rated"
    assert TEASER_ROLE_LOW == "lowest_rated"
    assert "gripe" not in TEASER_ROLE_LOW, (
        "the teaser ranks positive share; a criticism word here would claim a "
        "comparison nothing computed")


def test_the_renderer_knows_every_role_the_builder_emits():
    """The Python constants and the JavaScript label map must agree. Nothing
    enforced this before: build_site could rename a role and cards.js would
    silently render no micro-label, which degrades quietly enough that nobody
    would file a bug."""
    import re
    from pathlib import Path
    from build_site import TEASER_ROLE_MOST, TEASER_ROLE_HIGH, TEASER_ROLE_LOW

    js = (Path(__file__).resolve().parent.parent
          / 'browser' / 'js' / 'cards.js').read_text(encoding='utf-8')
    block = re.search(r'AXIS_ROLE_LABELS\s*=\s*\{(.*?)\}', js, re.S)
    assert block, "AXIS_ROLE_LABELS not found in cards.js"
    keys = set(re.findall(r'^\s*(\w+)\s*:', block.group(1), re.M))
    assert keys == {TEASER_ROLE_MOST, TEASER_ROLE_HIGH, TEASER_ROLE_LOW}


def test_the_teaser_carries_the_whole_triple():
    """A bar with only pos and neg invites reading the remainder as negative
    space rather than neutral judgement. Every share travels with its
    siblings -- the same rule the card body follows."""
    from build_site import teaser_entry
    card = _card([_axis("image_quality", pos=80, neg=20, neu=40),
                  _axis("battery_life", pos=10, neg=30, neu=20),
                  _axis("handling", pos=40, neg=15, neu=10)])
    entry = teaser_entry(card)
    assert entry["top_axes"], "no teaser axes to check"
    for axis in entry["top_axes"]:
        assert {"pos", "neu", "neg", "total"} <= set(axis), axis
        assert axis["pos"] + axis["neu"] + axis["neg"] <= axis["total"]
