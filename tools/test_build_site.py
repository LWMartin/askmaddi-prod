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


def test_empty_card_yields_no_axes():
    assert select_teaser_axes(_card([])) == []


# ─── Affiliate tag enforcement (revenue regression: untagged /dp/ CTAs) ─────
from build_site import ensure_affiliate_tag, new_cta, used_cta


def test_raw_amazon_product_url_gets_tagged():
    out = ensure_affiliate_tag("https://www.amazon.com/dp/B09JZT6XXX")
    assert "tag=askmaddi-20" in out


def test_already_tagged_amazon_url_not_doubled():
    url = "https://www.amazon.com/dp/B09JZT6XXX?tag=askmaddi-20"
    out = ensure_affiliate_tag(url)
    assert out.count("tag=askmaddi-20") == 1


def test_raw_ebay_item_url_gets_campaign_params():
    out = ensure_affiliate_tag("https://www.ebay.com/itm/123456789")
    assert "campid=5339138080" in out and "mkrid=711-53200-19255-0" in out


def test_non_program_domain_passthrough():
    url = "https://www.bhphotovideo.com/c/product/123"
    assert ensure_affiliate_tag(url) == url


def test_new_cta_tags_current_new_url_when_affiliate_url_null():
    card = {
        "identity": {"display_name": "Sony A7 IV"},
        "pricing": {
            "affiliate_url": None,
            "current_new_url": "https://www.amazon.com/dp/B09JZT6XXX",
            "current_new_usd": 2498,
        },
    }
    _, url = new_cta(card)
    assert "tag=askmaddi-20" in url


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


def test_new_cta_prefers_asin_dp_link_over_search():
    card = {
        "identity": {"display_name": "Sony A7 IV"},
        "pricing": {"amazon_asin": "B09JZT6YK5", "affiliate_url": None, "current_new_url": None},
    }
    _, url = new_cta(card)
    assert "/dp/B09JZT6YK5" in url
    assert "tag=askmaddi-20" in url
    assert "/s?k=" not in url


def test_new_cta_explicit_urls_outrank_asin():
    card = {
        "identity": {"display_name": "Sony A7 IV"},
        "pricing": {"amazon_asin": "B09JZT6YK5", "current_new_url": "https://www.amazon.com/dp/BEXPLICIT01"},
    }
    _, url = new_cta(card)
    assert "/dp/BEXPLICIT01" in url and "B09JZT6YK5" not in url


def test_new_cta_search_fallback_without_asin():
    # No ASIN, no EPID -> the ladder resolves to an EPN-tagged eBay search,
    # NOT Amazon search. Amazon search would dump the buyer on a results page
    # whose top hit is a different product (the e930bea CTA fix). This test is
    # the regression guard for that reroute: a no-ASIN card must land on eBay.
    card = {"identity": {"display_name": "Sony A7 IV"}, "pricing": {}}
    _, url = new_cta(card)
    assert "ebay.com" in url
    assert "campid=5339138080" in url
    assert "/s?k=" not in url  # must NOT fall back to Amazon search


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
