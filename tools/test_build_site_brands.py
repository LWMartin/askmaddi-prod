"""Tests for brand landing pages (browse-by-brand IA) in build_site.py.

Brand pages are derived whole-corpus artifacts: /brands/<slug>/ per brand plus
a /brands/ index, grouped by category, keyed by SLUG so casing variants merge
into one page. These tests lock the grouping/merge behaviour, the rendered
structure, and the sitemap wiring.
"""
import json
from pathlib import Path

import build_site as B


def card(cid, brand, category="body", subcategory="mirrorless",
         display=None, last_built="2026-08-01"):
    return {
        "card_id": cid,
        "identity": {
            "brand": brand,
            "display_name": display or f"{brand} {cid}",
            "category": category,
            "subcategory": subcategory,
            "image_thumb": f"https://img.example/{cid}.jpg",
        },
        "pricing": {"msrp_usd": 1000, "used_market": {"bands": {"pre_owned": 800}}},
        "freshness": {"last_built": last_built},
    }


# ─── brand_slug ──────────────────────────────────────────────────────────────

def test_brand_slug_basic():
    assert B.brand_slug("Sony") == "sony"
    assert B.brand_slug("Peak Design") == "peak-design"
    assert B.brand_slug("FUJIFILM") == "fujifilm"
    assert B.brand_slug("Konica Minolta") == "konica-minolta"


def test_brand_slug_strips_punctuation_and_edges():
    assert B.brand_slug("  Leica!  ") == "leica"
    assert B.brand_slug("Auto/Robotics") == "auto-robotics"


# ─── collect_brands ──────────────────────────────────────────────────────────

def test_collect_brands_merges_casing_variants():
    cards = [card("a", "Fujifilm"), card("b", "Fujifilm"), card("c", "FUJIFILM")]
    brands = B.collect_brands(cards)
    assert len(brands) == 1
    b = brands[0]
    assert b["slug"] == "fujifilm"
    # Most-frequent raw casing wins (Fujifilm 2 vs FUJIFILM 1).
    assert b["display"] == "Fujifilm"
    assert b["count"] == 3


def test_collect_brands_sorted_by_count_then_name():
    cards = ([card(f"s{i}", "Sony") for i in range(3)]
             + [card(f"c{i}", "Canon") for i in range(3)]
             + [card("n0", "Nikon")])
    brands = B.collect_brands(cards)
    # Sony and Canon tie at 3 -> alphabetical; Nikon last at 1.
    assert [b["display"] for b in brands] == ["Canon", "Sony", "Nikon"]


def test_collect_brands_skips_cards_without_brand():
    cards = [card("a", "Sony"), card("b", "")]
    cards[1]["identity"]["brand"] = None
    brands = B.collect_brands(cards)
    assert [b["display"] for b in brands] == ["Sony"]
    assert brands[0]["count"] == 1


def test_collect_brands_groups_by_category():
    cards = [card("a", "Sony", category="body"),
             card("b", "Sony", category="lens", subcategory="prime"),
             card("c", "Sony", category="weird_new_type")]
    b = B.collect_brands(cards)[0]
    assert len(b["by_category"]["body"]) == 1
    assert len(b["by_category"]["lens"]) == 1
    # Unknown category folds into the _other bucket, never dropped.
    assert len(b["by_category"]["_other"]) == 1


# ─── category sections / phrase ──────────────────────────────────────────────

def test_brand_category_sections_ordered():
    cards = [card("l", "Sony", category="lens", subcategory="prime"),
             card("b", "Sony", category="body")]
    b = B.collect_brands(cards)[0]
    labels = [lbl for lbl, _c, _cs in B._brand_category_sections(b)]
    # body ("Cameras") precedes lens ("Lenses") regardless of input order.
    assert labels == ["Cameras", "Lenses"]


def test_brand_category_phrase():
    cards = [card("b", "Sony", category="body"),
             card("l", "Sony", category="lens", subcategory="prime")]
    b = B.collect_brands(cards)[0]
    assert B._brand_category_phrase(b) == "cameras and lenses"
    single = B.collect_brands([card("b", "Sony", category="body")])[0]
    assert B._brand_category_phrase(single) == "cameras"


# ─── render_brand_page ───────────────────────────────────────────────────────

def test_render_brand_page_structure():
    cards = [card("sony-a7-v", "Sony", category="body"),
             card("sony-85mm", "Sony", category="lens", subcategory="prime")]
    b = B.collect_brands(cards)[0]
    html = B.render_brand_page(b)
    assert "<title>Sony cameras and lenses — 2 reviewed" in html
    assert "<h1>Sony — 2 reviewed</h1>" in html
    assert 'href="/cards/sony-a7-v/"' in html
    assert 'href="/cards/sony-85mm/"' in html
    assert "Cameras (1)" in html and "Lenses (1)" in html
    assert 'href="/brands/"' in html                 # back-to-all-brands link
    assert "affiliate-disclosure-bar" in html
    assert f'rel="canonical" href="{B.BASE_URL}/brands/sony/"' in html


def test_render_brand_page_jsonld_valid_collectionpage():
    b = B.collect_brands([card("sony-a7-v", "Sony")])[0]
    html = B.render_brand_page(b)
    start = html.index('application/ld+json">') + len('application/ld+json">')
    doc = json.loads(html[start:html.index("</script>", start)])
    assert doc["@type"] == "CollectionPage"
    assert doc["mainEntity"]["@type"] == "ItemList"
    assert doc["mainEntity"]["numberOfItems"] == 1
    assert doc["mainEntity"]["itemListElement"][0]["url"] == f"{B.BASE_URL}/cards/sony-a7-v/"


def test_render_brand_page_has_dual_ctas_per_card():
    b = B.collect_brands([card("sony-a7-v", "Sony")])[0]
    html = B.render_brand_page(b)
    assert "btn-buy-new" in html and "btn-buy-used" in html
    # CTAs are affiliate-tagged and nofollow-sponsored (SEO + disclosure).
    assert 'rel="nofollow noopener sponsored"' in html


# ─── render_brands_index ─────────────────────────────────────────────────────

def test_render_brands_index_tiles_and_pluralisation():
    cards = ([card(f"s{i}", "Sony") for i in range(2)]
             + [card("l0", "Leica", category="lens", subcategory="prime")])
    brands = B.collect_brands(cards)
    html = B.render_brands_index(brands)
    assert 'href="/brands/sony/"' in html
    assert 'href="/brands/leica/"' in html
    assert "2 products" in html          # Sony
    assert "1 product" in html           # Leica (singular)
    assert "1 lens" in html and "1 lenses" not in html   # singular breakdown


# ─── write_brand_pages + sitemap wiring ──────────────────────────────────────

def test_write_brand_pages_emits_files(tmp_path):
    cards = [card("sony-a7-v", "Sony"), card("canon-r5", "Canon")]
    brands = B.write_brand_pages(tmp_path, cards)
    assert (tmp_path / "brands" / "index.html").exists()
    assert (tmp_path / "brands" / "sony" / "index.html").exists()
    assert (tmp_path / "brands" / "canon" / "index.html").exists()
    assert {b["slug"] for b in brands} == {"sony", "canon"}


def test_sitemap_includes_brand_urls_only_when_passed(tmp_path):
    cards = [card("sony-a7-v", "Sony")]
    brands = B.collect_brands(cards)
    B.write_sitemap(tmp_path, cards, brands=brands)
    xml = (tmp_path / "sitemap.xml").read_text()
    assert f"{B.BASE_URL}/brands/" in xml
    assert f"{B.BASE_URL}/brands/sony/" in xml

    # Without brands, no brand URLs leak into the sitemap.
    B.write_sitemap(tmp_path, cards)
    xml2 = (tmp_path / "sitemap.xml").read_text()
    assert "/brands/" not in xml2
