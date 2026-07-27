"""Tests for the Phase 0 distribution seams in build_site (2026-07-17):
retailer_from_url hostname discipline, llms.txt emission, and the page
instrumentation (body data-category, tagged CTAs, beacon include, subscribe
form with honeypot).

Run from repo root:  python -m pytest tools/test_build_site_phase0.py -q
"""
import re
import sys
from datetime import datetime, timedelta, timezone
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


def _amazon_anchor_text(html):
    """Anchor TEXT of the Amazon rung, or '' if there is no rung.

    Deliberately scoped to the text between '>' and '</a>'. Matching to the
    next '<' instead would run past the opening tag and swallow the href,
    whose ASIN and tracking tag contain digits — the exact false positive the
    DEPLOYMENT.md tripwire hit on 2026-07-27.
    """
    m = re.search(r'btn-buy-amazon[^>]*>([^<]*)</a>', html)
    return m.group(1) if m else ""


def test_amazon_rung_rendered_and_tagged():
    card = _card()
    card["pricing"]["amazon_asin"] = "B09JZT6YK5"
    html = render_page(card)
    assert 'class="btn-affiliate btn-buy-amazon"' in html
    assert 'href="https://www.amazon.com/dp/B09JZT6YK5?tag=askmaddi20-20"' in html
    assert 'data-retailer="amazon"' in html


def test_rendered_amazon_rung_carries_no_number():
    """THE compliance invariant, asserted on the MARKUP rather than the helper.

    Without Creators API credentials the Associates agreement forbids showing
    Amazon price, availability, star rating or review count. A card stuffed
    with prices must still render a wholly numeric-free Amazon label.
    """
    card = _card()
    card["pricing"].update({
        "amazon_asin": "B09JZT6YK5",
        "current_new_usd": 2498,
        "msrp_usd": 2498,
        "used_market": {"bands": {"good": 1450}},
    })
    text = _amazon_anchor_text(render_page(card))
    assert text.strip().startswith("See price on Amazon")
    assert not re.search(r"[0-9]", text), f"number leaked onto Amazon rung: {text!r}"
    assert "$" not in text


def test_absent_card_renders_no_amazon_rung():
    card = _card()
    card["pricing"]["amazon_absent"] = True
    card["pricing"]["amazon_asin"] = "B09JZT6YK5"  # absent verdict must win
    html = render_page(card)
    assert "btn-buy-amazon" not in html
    assert "amazon.com" not in html


def test_dead_tag_absent_from_rendered_page():
    card = _card()
    card["pricing"]["amazon_asin"] = "B09JZT6YK5"
    html = render_page(card)
    assert not re.search(r"tag=askmaddi-20(?![0-9])", html)


def test_stale_card_reports_its_age_honestly():
    """Staleness is computed at RENDER, not read from the card.

    freshness.staleness_days is frozen at build time and reads 0 on a card
    five weeks old. A card built 40 days ago must say so on the page — the
    whole point of surfacing freshness is defeated if the number can lie.
    """
    card = _card()
    old = datetime.now(timezone.utc) - timedelta(days=40)
    card["freshness"] = {
        "last_built": old.isoformat(),
        "staleness_days": 0,          # the frozen lie; must be ignored
        "source_count": 7,
    }
    html = render_page(card)
    assert "40 days ago" in html
    assert "Analysis as of" in html


def test_fresh_card_says_today():
    card = _card()
    card["freshness"] = {
        "last_built": datetime.now(timezone.utc).isoformat(),
        "source_count": 7,
    }
    assert "(today)" in render_page(card)


def test_synthesis_and_price_clocks_are_labelled_apart():
    """The two clocks must never collapse into one 'Last updated' stamp.

    Prices refresh nightly and cheaply; synthesis re-extraction does not scale
    nightly at catalog size. One undifferentiated stamp would let a nightly
    price tick appear to vouch for month-old synthesis.
    """
    card = _card()
    old = datetime.now(timezone.utc) - timedelta(days=30)
    card["freshness"] = {"last_built": old.isoformat(), "source_count": 7}
    card["pricing"]["used_market"] = {
        "price_updated_at": datetime.now(timezone.utc).isoformat(),
        "bands": {"good": 900},
    }
    html = render_page(card)
    assert "Analysis as of" in html and "30 days ago" in html
    assert "Used price as of" in html
    # The ambiguous phrasing must not come back.
    assert "Last updated" not in html


def test_missing_last_built_degrades_silently():
    # Honest absence: no date claim at all rather than a build-time fallback.
    card = _card()
    card["freshness"] = {"source_count": 7}
    html = render_page(card)
    assert "Analysis as of" not in html
    assert "days ago" not in html


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
