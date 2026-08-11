"""Holdout — vs-pages: deterministic comparison surface (maddi-distribution v2.0 §8).

A vs-page is a DETERMINISTIC JOIN of two published cards on their shared review
axes. It is the highest-intent citation surface we can build ("<A> vs <B>" is the
transactional query class Perplexity/AI answer engines field), and it is unlocked
now that the catalogue passed the >=10-card gate.

The load-bearing test in this file is NO EDITORIAL VERDICT. A comparison page is
the single strongest temptation to editorialise — the reader wants to be told
which to buy — and the whole brand promise ("we aggregate others' assessments and
never write our own opinions") dies the moment the page ranks the two products.
So the page restates each card's grounded numbers side by side and uses no verdict
vocabulary at all, not even "no winner" framing (the word itself is banned).

The other invariants: the join is EXACTLY the intersection of visible axes (never
an axis only one card covers); every share is grounded (pos/neu/neg + denominator,
inheriting the Phase-1 rule); both cards' affiliate CTAs and the disclosure are
present (this is the revenue surface); the slug is canonical/symmetric so A-vs-B
and B-vs-A collapse to one page; and pair selection is same-category, min-shared
gated, and deterministic.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_vs_pages as V  # noqa: E402


# ── fixtures ────────────────────────────────────────────────────────────────
def _axis(axis_id, display, pos, neu, neg):
    return {"axis_id": axis_id, "display_name": display,
            "sentiment": {"pos": pos, "neu": neu, "neg": neg,
                          "total": pos + neu + neg}}


def _card(card_id, name, brand, category, lead, detail, created):
    return {
        "card_id": card_id, "card_version": "1.0", "vertical": "photography",
        "identity": {"display_name": name, "brand": brand, "category": category,
                     "subcategory": "", "year_introduced": 2024},
        "freshness": {"last_built": "2026-08-05T10:00:00+00:00",
                      "created_at": created, "source_count": 20},
        "pricing": {}, "confidence": {"overall": "medium"},
        "lead_axes": lead, "detail_axes": detail,
        "synthesis": {"consensus_paragraph": f"Reviewers discuss the {name}."},
        "axis_roles": {}, "sources": [],
        "facts": {"specs": {}, "provenance": {}, "conflicts": []},
    }


# Two bodies sharing autofocus + video (each a full 100-claim split); each also
# owns a private axis that must NOT enter the join.
CARD_A = _card(
    "alpha-one", "Alpha One", "Acme", "body",
    [_axis("autofocus", "Autofocus", 60, 20, 20),
     _axis("video", "Video", 30, 10, 60)],
    [_axis("stabilization", "Stabilization", 40, 40, 20)],
    "2026-07-01T00:00:00+00:00")
CARD_B = _card(
    "beta-two", "Beta Two", "Beta", "body",
    [_axis("autofocus", "Autofocus", 50, 25, 25),
     _axis("video", "Video", 45, 15, 40)],
    [_axis("ergonomics", "Ergonomics", 70, 20, 10)],
    "2026-07-10T00:00:00+00:00")
# A third body that shares only ONE axis with the others (min-shared gate bait).
CARD_D = _card(
    "delta-four", "Delta Four", "Delta", "body",
    [_axis("autofocus", "Autofocus", 55, 20, 25)],
    [_axis("battery", "Battery", 30, 30, 40)],
    "2026-07-05T00:00:00+00:00")
# A lens — different category, must never pair with a body.
CARD_C = _card(
    "gamma-lens", "Gamma Lens", "Gamma", "lens",
    [_axis("optics", "Optics", 80, 10, 10),
     _axis("build", "Build", 60, 20, 20)],
    [], "2026-07-03T00:00:00+00:00")


# ── the join is exactly the intersection of visible axes ────────────────────
def test_shared_axes_are_the_intersection():
    ids = {a["axis_id"] for a in V.shared_axes(CARD_A, CARD_B)}
    assert ids == {"autofocus", "video"}
    assert "stabilization" not in ids  # A-only
    assert "ergonomics" not in ids     # B-only


def test_shared_axes_carry_both_cards_triples():
    by = {a["axis_id"]: a for a in V.shared_axes(CARD_A, CARD_B)}
    af = by["autofocus"]
    assert af["a"]["pos"] == 60 and af["a"]["total"] == 100
    assert af["b"]["pos"] == 50 and af["b"]["total"] == 100
    vid = by["video"]
    assert vid["a"]["neg"] == 60 and vid["b"]["neg"] == 40


def test_shared_axes_order_is_deterministic():
    order = [a["axis_id"] for a in V.shared_axes(CARD_A, CARD_B)]
    # combined-total tie (200 each) breaks by axis_id ascending
    assert order == ["autofocus", "video"]
    # symmetric membership regardless of argument order
    assert {a["axis_id"] for a in V.shared_axes(CARD_B, CARD_A)} == set(order)


# ── canonical, symmetric slug ───────────────────────────────────────────────
def test_vs_slug_canonical_and_symmetric():
    assert V.vs_slug(CARD_A, CARD_B) == "alpha-one-vs-beta-two"
    assert V.vs_slug(CARD_B, CARD_A) == "alpha-one-vs-beta-two"


# ── the rendered page ───────────────────────────────────────────────────────
def test_page_has_both_names_and_only_shared_axes():
    html = V.render_vs_page(CARD_A, CARD_B)
    assert "Alpha One" in html and "Beta Two" in html
    assert "Autofocus" in html and "Video" in html
    # a private axis must not surface as a comparison row anywhere on the page
    assert "Stabilization" not in html and "Ergonomics" not in html


def test_every_share_is_grounded():
    # Phase-1 rule inherited: a positive share never appears without its
    # neutral and negative siblings. Counting the three labels is the cheapest
    # faithful proxy for "they always travel together".
    html = V.render_vs_page(CARD_A, CARD_B)
    assert html.count("% positive") >= 4  # 2 axes x 2 cards
    assert html.count("% positive") == html.count("% neutral") == html.count("% negative")
    # and a denominator is present on the axis rows
    assert re.search(r"of \d+ claims", html)


def test_no_editorial_verdict():
    # THE load-bearing check. The page must not rank, pick, or recommend one
    # product over the other — not even via "no winner" framing (the token
    # itself is banned so the vocabulary never appears on the surface).
    html = V.render_vs_page(CARD_A, CARD_B).lower()
    banned = [
        "winner", "wins", "better buy", "best buy", "we recommend",
        "our pick", "our choice", "should buy", "superior", "beats",
        "outclasses", "verdict", "clear choice", "edge goes to",
        "recommend buying", "the better camera", "is the better",
    ]
    hits = [b for b in banned if b in html]
    assert hits == [], f"editorial verdict vocabulary leaked onto vs-page: {hits}"


def test_both_affiliate_ctas_and_disclosure_present():
    html = V.render_vs_page(CARD_A, CARD_B)
    # two cards x (new + used) = four sponsored outbound links
    assert html.count("sponsored") >= 4
    # each card links back to its own dossier (the free-service anchor)
    assert "/cards/alpha-one/" in html and "/cards/beta-two/" in html
    # affiliate disclosure surfaced (brand promise + FTC)
    low = html.lower()
    assert "disclosure" in low or "commission" in low


def test_canonical_url_and_jsonld():
    html = V.render_vs_page(CARD_A, CARD_B)
    assert "/vs/alpha-one-vs-beta-two/" in html
    assert "application/ld+json" in html
    # both products are described in structured data
    assert html.count('"Product"') >= 2 or '"ItemList"' in html


def test_year_and_asof_stamp_surfaced():
    html = V.render_vs_page(CARD_A, CARD_B)
    assert "2026" in html                         # freshness / analysis year
    assert re.search(r"[Aa]s of|[Uu]pdated|[Aa]nalys", html)  # a dated stamp


def test_render_is_byte_deterministic():
    assert V.render_vs_page(CARD_A, CARD_B) == V.render_vs_page(CARD_A, CARD_B)


# ── pair selection ──────────────────────────────────────────────────────────
def test_select_pairs_same_category_min_shared_canonical():
    pairs = V.select_pairs([CARD_A, CARD_B, CARD_D, CARD_C], min_shared=2)
    slugs = {V.vs_slug(x, y) for (x, y) in pairs}
    # only A-B clears same-category + >=2 shared axes; A/B-vs-D share 1;
    # the lens never pairs with a body.
    assert slugs == {"alpha-one-vs-beta-two"}
    assert len(pairs) == 1


def test_select_pairs_is_deterministic_regardless_of_input_order():
    forward = V.select_pairs([CARD_A, CARD_B, CARD_D, CARD_C], min_shared=2)
    reverse = V.select_pairs([CARD_C, CARD_D, CARD_B, CARD_A], min_shared=2)
    norm = lambda ps: [V.vs_slug(a, b) for (a, b) in ps]
    assert norm(forward) == norm(reverse)
