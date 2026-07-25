"""Tests for the Phase 0 distribution seams in build_site (2026-07-17):
retailer_from_url hostname discipline, llms.txt emission, and the page
instrumentation (body data-category, tagged CTAs, beacon include, subscribe
form with honeypot).

Run from repo root:  python -m pytest tools/test_build_site_phase0.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_site import (  # noqa: E402
    retailer_from_url, write_llms_txt, render_page,
)


# ─── retailer_from_url: hostname match, never substring-in-url ──────────────

def test_retailer_amazon_hosts():
    assert retailer_from_url("https://www.amazon.com/dp/B0ABC") == "amazon"
    assert retailer_from_url("https://amazon.com/s?k=x") == "amazon"


def test_retailer_ebay_hosts():
    assert retailer_from_url("https://www.ebay.com/sch/i.html?_nkw=x") == "ebay"


def test_retailer_adorama_hosts():
    # Partnerize deep-link (the wrapped new-goods CTA), generic short link,
    # and an un-wrapped direct destination all classify as adorama.
    assert retailer_from_url(
        "https://adorama.prf.hn/click/camref:1101l5Pw9q/destination:"
        "https://www.adorama.com/l/?searchinfo=x") == "adorama"
    assert retailer_from_url("https://prf.hn/l/PlDklJx/") == "adorama"
    assert retailer_from_url("https://www.adorama.com/l/?searchinfo=x") == "adorama"


def test_retailer_query_mention_does_not_count():
    # 'amazon' in the QUERY of another host must not classify as amazon.
    assert retailer_from_url(
        "https://example.com/out?to=amazon.com") == "other"
    # ...and a hostname merely CONTAINING the brand is not the brand.
    assert retailer_from_url("https://notamazon.com/x") == "other"
    assert retailer_from_url("https://amazon.com.evil.io/x") == "other"


def test_retailer_junk_tolerated():
    assert retailer_from_url("") == "other"
    assert retailer_from_url(None) == "other"
    assert retailer_from_url("not a url") == "other"


# ─── fixtures ───────────────────────────────────────────────────────────────

def _card(cid="test-cam", category="camera"):
    return {
        "card_id": cid,
        "identity": {
            "display_name": "Test Cam X100",
            "brand": "Test",
            "category": category,
            "subcategory": "mirrorless",
        },
        "synthesis": {"consensus_paragraph": "Reviewers broadly like it."},
        "sources": [{"title": "Review A", "url": "https://example.com/a"}],
        "freshness": {"source_count": 7},
        "confidence": {"overall": "medium"},
        "lead_axes": [],
        "detail_axes": [],
        "pricing": {},
    }


# ─── llms.txt ───────────────────────────────────────────────────────────────

def test_llms_txt_lists_cards_and_method(tmp_path):
    path = write_llms_txt(tmp_path, [_card("b-cam"), _card("a-cam")])
    text = path.read_text()
    assert path.name == "llms.txt"
    assert text.startswith("# AskMaddi")
    # Sorted by card_id, absolute URLs, source counts surfaced.
    a = text.index("https://askmaddi.com/cards/a-cam/")
    b = text.index("https://askmaddi.com/cards/b-cam/")
    assert a < b
    assert "from 7 sources" in text
    assert "https://askmaddi.com/why.html" in text
    assert "https://askmaddi.com/mission.html" in text


# ─── page instrumentation ───────────────────────────────────────────────────

def test_page_body_carries_category():
    html = render_page(_card())
    assert '<body data-category="camera">' in html


def test_page_ctas_tagged_for_beacon():
    html = render_page(_card())
    # Both hero CTAs carry data-out + a whitelisted-shape retailer + category.
    assert html.count("data-out") >= 2
    assert 'data-category="camera"' in html
    assert 'data-retailer="' in html


def test_page_includes_beacon_and_subscribe():
    html = render_page(_card())
    assert '<script src="/js/beacon.js" defer></script>' in html
    assert "form data-subscribe" in html
    # Honeypot present, named 'website', visually hidden.
    assert 'name="website"' in html
    assert 'name="email"' in html


def test_page_category_lowercased_and_defaulted():
    html = render_page(_card(category="CAMERA"))
    assert '<body data-category="camera">' in html
    card = _card()
    card["identity"]["category"] = ""
    html2 = render_page(card)
    assert '<body data-category="unknown">' in html2
