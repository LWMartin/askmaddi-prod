"""C6 (use-case-guides Phase-0): review-authority JSON-LD, rating-free.

Doctrine (Lee 2026-08-17): make review coverage machine-visible WITHOUT a
rating number. Emit attributed Review items + pros/cons as sourced counts;
never ratingValue / AggregateRating (a number is a verdict). These tests pin
that contract so a future 'just add stars for rich results' change fails loud.

Run from repo root:  python -m pytest tools/test_build_site_c6_review_authority.py -q
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_site import (  # noqa: E402
    schema_reviews, schema_pros_cons, schema_org_jsonld,
    _source_reviewer_map,
)


def _axis(name, pos, neu, neg, sources=None, fq=None):
    a = {
        "display_name": name,
        "axis_id": name.lower().replace(" ", "_"),
        "sentiment": {"pos": pos, "neu": neu, "neg": neg,
                      "total": pos + neu + neg,
                      "sources": sources or []},
    }
    if fq is not None:
        a["face_quote"] = fq
    return a


def _card():
    return {
        "card_id": "canon-r6",
        "identity": {"display_name": "Canon EOS R6", "brand": "Canon",
                     "category": "camera"},
        "sources": [
            {"source_id": "rss-dpreview-x", "reviewer": "DPReview (GPS Media)"},
            {"source_id": "youtube-dustin", "reviewer": "Dustin Abbott"},
        ],
        "lead_axes": [
            _axis("Autofocus", 20, 3, 2,
                  sources=[{"source_id": "rss-dpreview-x"},
                           {"source_id": "youtube-dustin"}],
                  fq={"quote_excerpt": "AF is excellent.", "url": "https://d/1",
                      "source_id": "rss-dpreview-x"}),
            _axis("Battery Life", 2, 1, 9,
                  sources=[{"source_id": "youtube-dustin"}],
                  fq={"quote_excerpt": "Battery drains fast.", "url": "https://d/2",
                      "source_id": "youtube-dustin"}),
        ],
        "detail_axes": [],
        "pricing": {},
        "freshness": {},
    }


# ─── the load-bearing invariant: no rating number, ever ──────────────────────

def test_no_rating_value_anywhere_in_jsonld():
    obj = json.loads(schema_org_jsonld(
        _card(), "https://askmaddi.com/cards/canon-r6/", "", ""))
    blob = json.dumps(obj)
    assert "ratingValue" not in blob
    assert "AggregateRating" not in blob
    assert "reviewRating" not in blob
    assert "bestRating" not in blob


# ─── attributed Review items ─────────────────────────────────────────────────

def test_reviews_attributed_to_real_sources_not_askmaddi():
    reviews = schema_reviews(_card(), _source_reviewer_map(_card()["sources"]))
    names = {r["author"]["name"] for r in reviews}
    assert "DPReview (GPS Media)" in names
    assert "Dustin Abbott" in names
    assert "AskMaddi" not in names          # we witness, we don't review
    for r in reviews:
        assert r["@type"] == "Review"
        assert "reviewRating" not in r      # quote it, never score it
        assert r["reviewBody"]
        assert r["reviewAspect"]


def test_reviews_resolve_curated_name_not_slug_garble():
    reviews = schema_reviews(_card(), _source_reviewer_map(_card()["sources"]))
    body_authors = json.dumps(reviews)
    assert "Rss Dpreview" not in body_authors


def test_sponsor_read_dropped_from_structured_reviews():
    card = _card()
    card["lead_axes"][0]["face_quote"]["quote_excerpt"] = (
        "Well, Storyblocks has you covered with 4K stock footage.")
    reviews = schema_reviews(card, _source_reviewer_map(card["sources"]))
    assert all("storyblocks" not in r["reviewBody"].lower() for r in reviews)


def test_genuine_review_with_spec_talk_is_kept():
    # Guard must be narrow: a real quote that happens to mention specs stays.
    card = _card()
    card["lead_axes"][0]["face_quote"]["quote_excerpt"] = (
        "Similar specs to the mark II but the autofocus is a real step up.")
    reviews = schema_reviews(card, _source_reviewer_map(card["sources"]))
    assert any("autofocus is a real step up" in r["reviewBody"].lower()
               for r in reviews)


def test_axis_without_face_quote_yields_no_review():
    card = _card()
    card["lead_axes"][0].pop("face_quote")
    reviews = schema_reviews(card, _source_reviewer_map(card["sources"]))
    assert all(r["reviewAspect"] != "Autofocus" for r in reviews)


# ─── pros / cons as sourced counts ───────────────────────────────────────────

def test_pros_and_cons_split_by_net_sentiment():
    pos, neg = schema_pros_cons(_card())
    pro_text = json.dumps(pos)
    con_text = json.dumps(neg)
    assert "Autofocus" in pro_text          # net-positive -> a strength
    assert "Battery Life" in con_text       # net-negative -> a weakness
    assert "Autofocus" not in con_text
    assert "Battery Life" not in pro_text


def test_proscon_text_is_counts_not_a_score():
    pos, _ = schema_pros_cons(_card())
    af = pos["itemListElement"][0]["name"]
    assert "20 positive vs 2 critical mentions" in af
    assert "2 sources" in af
    # No star/score vocabulary leaked in.
    for bad in ("star", "rating", "/5", "out of", "score"):
        assert bad not in af.lower()


def test_thin_axis_is_silent():
    # total < _PROSCON_MIN_TOTAL must not produce a pro/con at all.
    card = _card()
    card["lead_axes"] = [_axis("Flash", 1, 0, 1)]  # total=2, below floor
    pos, neg = schema_pros_cons(card)
    assert pos is None and neg is None


def test_balanced_axis_is_silent():
    card = _card()
    card["lead_axes"] = [_axis("Ergonomics", 5, 2, 5)]  # net == 0
    pos, neg = schema_pros_cons(card)
    assert pos is None and neg is None


def test_notes_absent_from_jsonld_when_no_evidence():
    card = _card()
    card["lead_axes"] = [_axis("Flash", 1, 0, 1)]   # too thin
    card["detail_axes"] = []
    obj = json.loads(schema_org_jsonld(
        card, "https://askmaddi.com/cards/x/", "", ""))
    assert "positiveNotes" not in obj
    assert "negativeNotes" not in obj


def test_full_jsonld_carries_review_and_notes():
    obj = json.loads(schema_org_jsonld(
        _card(), "https://askmaddi.com/cards/canon-r6/", "", ""))
    assert obj["@type"] == "Product"
    assert isinstance(obj["review"], list) and obj["review"]
    assert obj["positiveNotes"]["@type"] == "ItemList"
    assert obj["negativeNotes"]["@type"] == "ItemList"
