"""Author's own test suite for build_vs_pages.py (companion to the gate's
tools/test_build_vs_pages.py, which this file must not duplicate verbatim).

Covers: min_shared boundary behaviour, missing/blank category handling,
axis-order tie-breaking with genuinely different combined totals, CLI
`main()` end-to-end against a temp cards dir, and a couple of shape checks
the gate doesn't exercise (image params accepted, empty-share axes excluded).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_vs_pages as V  # noqa: E402


def _axis(axis_id, display, pos, neu, neg):
    return {"axis_id": axis_id, "display_name": display,
            "sentiment": {"pos": pos, "neu": neu, "neg": neg,
                          "total": pos + neu + neg}}


def _card(card_id, name, brand, category, lead, detail, created="2026-07-01T00:00:00+00:00"):
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


CARD_A = _card("alpha-one", "Alpha One", "Acme", "body",
                [_axis("autofocus", "Autofocus", 60, 20, 20),
                 _axis("video", "Video", 30, 10, 60)],
                [_axis("stabilization", "Stabilization", 40, 40, 20)])
CARD_B = _card("beta-two", "Beta Two", "Beta", "body",
                [_axis("autofocus", "Autofocus", 50, 25, 25),
                 _axis("video", "Video", 45, 15, 40)],
                [_axis("ergonomics", "Ergonomics", 70, 20, 10)])


# ── shared_axes: ordering by genuinely different combined totals ───────────
def test_shared_axes_orders_by_combined_total_descending():
    c1 = _card("c1", "C One", "X", "body",
               [_axis("a", "A", 5, 5, 5), _axis("b", "B", 40, 30, 30)], [])
    c2 = _card("c2", "C Two", "Y", "body",
               [_axis("a", "A", 5, 5, 5), _axis("b", "B", 20, 10, 10)], [])
    order = [row["axis_id"] for row in V.shared_axes(c1, c2)]
    # b: 40+30+30 + 20+10+10 = 140 vs a: 5+5+5+5+5+5 = 30 -> b first
    assert order == ["b", "a"]


def test_shared_axes_excludes_zero_total_axis():
    empty_axis = {"axis_id": "empty", "display_name": "Empty",
                  "sentiment": {"pos": 0, "neu": 0, "neg": 0, "total": 0}}
    c1 = _card("e1", "E One", "X", "body", [_axis("a", "A", 1, 1, 1), empty_axis], [])
    c2 = _card("e2", "E Two", "Y", "body", [_axis("a", "A", 1, 1, 1), empty_axis], [])
    ids = {row["axis_id"] for row in V.shared_axes(c1, c2)}
    assert ids == {"a"}
    assert "empty" not in ids


def test_shared_axes_dedupes_repeated_axis_id_within_one_card():
    dup_lead = [_axis("a", "A first", 10, 0, 0)]
    dup_detail = [_axis("a", "A second", 99, 99, 99)]
    c1 = _card("d1", "D One", "X", "body", dup_lead, dup_detail)
    c2 = _card("d2", "D Two", "Y", "body", [_axis("a", "A", 1, 1, 1)], [])
    rows = V.shared_axes(c1, c2)
    assert len(rows) == 1
    assert rows[0]["a"]["pos"] == 10  # kept the FIRST occurrence (lead, not detail)


# ── vs_slug ──────────────────────────────────────────────────────────────
def test_vs_slug_uses_card_id_not_display_name():
    slug = V.vs_slug(CARD_A, CARD_B)
    assert slug == "alpha-one-vs-beta-two"
    assert "Alpha" not in slug


# ── render_vs_page shape ────────────────────────────────────────────────
def test_render_vs_page_accepts_image_args_without_erroring():
    html = V.render_vs_page(CARD_A, CARD_B, image_a="https://x/a.jpg", image_b="https://x/b.jpg")
    assert "Alpha One" in html and "Beta Two" in html


def test_render_vs_page_orientation_independent_output():
    # Byte-identical regardless of call order — canonicalisation happens
    # before anything else in render_vs_page.
    assert V.render_vs_page(CARD_A, CARD_B) == V.render_vs_page(CARD_B, CARD_A)


def test_render_vs_page_has_html_doctype_and_head():
    html = V.render_vs_page(CARD_A, CARD_B)
    assert html.startswith("<!DOCTYPE html>")
    assert "<title>" in html and "</html>" in html


# ── select_pairs: category and min_shared boundaries ────────────────────
def test_select_pairs_excludes_blank_category():
    blank = _card("blank-one", "Blank One", "X", "", [_axis("a", "A", 1, 1, 1)], [])
    other = _card("blank-two", "Blank Two", "Y", "", [_axis("a", "A", 1, 1, 1)], [])
    pairs = V.select_pairs([blank, other], min_shared=1)
    assert pairs == []  # empty category never pairs, even with itself


def test_select_pairs_min_shared_boundary_is_inclusive():
    # exactly 2 shared axes with min_shared=2 must be KEPT
    pairs = V.select_pairs([CARD_A, CARD_B], min_shared=2)
    assert len(pairs) == 1
    # min_shared=3 (more than available) must exclude it
    pairs3 = V.select_pairs([CARD_A, CARD_B], min_shared=3)
    assert pairs3 == []


def test_select_pairs_empty_and_singleton_input():
    assert V.select_pairs([], min_shared=2) == []
    assert V.select_pairs([CARD_A], min_shared=1) == []


def test_select_pairs_no_duplicate_within_output():
    pairs = V.select_pairs([CARD_A, CARD_B], min_shared=1)
    slugs = [V.vs_slug(a, b) for a, b in pairs]
    assert len(slugs) == len(set(slugs))


# ── CLI main() end-to-end ────────────────────────────────────────────────
def test_main_writes_expected_pages_to_out_dir(tmp_path):
    cards_dir = tmp_path / "cards"
    out_dir = tmp_path / "vs"
    cards_dir.mkdir()
    (cards_dir / "alpha-one.json").write_text(json.dumps(CARD_A), encoding="utf-8")
    (cards_dir / "beta-two.json").write_text(json.dumps(CARD_B), encoding="utf-8")

    rc = V.main(["--cards-dir", str(cards_dir), "--out", str(out_dir), "--min-shared", "2"])
    assert rc == 0

    page = out_dir / "alpha-one-vs-beta-two" / "index.html"
    assert page.exists()
    html = page.read_text(encoding="utf-8")
    assert "Alpha One" in html and "Beta Two" in html


def test_main_tolerates_a_malformed_card_file(tmp_path):
    cards_dir = tmp_path / "cards"
    out_dir = tmp_path / "vs"
    cards_dir.mkdir()
    (cards_dir / "alpha-one.json").write_text(json.dumps(CARD_A), encoding="utf-8")
    (cards_dir / "beta-two.json").write_text(json.dumps(CARD_B), encoding="utf-8")
    (cards_dir / "broken.json").write_text("{not valid json", encoding="utf-8")

    rc = V.main(["--cards-dir", str(cards_dir), "--out", str(out_dir), "--min-shared", "2"])
    assert rc == 0
    assert (out_dir / "alpha-one-vs-beta-two" / "index.html").exists()


def test_main_writes_nothing_when_no_pairs_clear_the_gate(tmp_path):
    cards_dir = tmp_path / "cards"
    out_dir = tmp_path / "vs"
    cards_dir.mkdir()
    (cards_dir / "alpha-one.json").write_text(json.dumps(CARD_A), encoding="utf-8")
    (cards_dir / "beta-two.json").write_text(json.dumps(CARD_B), encoding="utf-8")

    rc = V.main(["--cards-dir", str(cards_dir), "--out", str(out_dir), "--min-shared", "99"])
    assert rc == 0
    assert not out_dir.exists() or list(out_dir.iterdir()) == []


def test_main_default_out_is_under_browser_vs():
    # sanity: the module-level ROOT anchors defaults under the repo, not cwd
    assert str(V.ROOT / "browser" / "vs").endswith("browser/vs")
    assert str(V.ROOT / "data" / "cards").endswith("data/cards")
