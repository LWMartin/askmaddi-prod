"""Parity holdout for the vendored gateway search_cells rerank (Lane A, Rung 1).

Pins the MODEL-ANCHORED relevance in the gateway copy to the SAME query ->
expected-result contract as the phantom-ops canonical ingest/search_rerank.py and
the browser precise.js. If this drifts, the "exact one, no wrong variant" promise
silently breaks server-side. See note
2026-08-28-askmaddi-positioning-precise-gear-research.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import search_cells as sc  # noqa: E402


def _row(name, price="100.00", url="https://x/0"):
    return {"name": name, "price": price, "url": url}


def test_variant_firewall_rejects_siblings():
    rows = [_row("Sony A7R IV Camera"), _row("Sony A7 III Camera"),
            _row("Sony A7 IV Camera")]
    assert [r["name"] for r in sc.rerank("sony a7 iv", rows)] == ["Sony A7 IV Camera"]


def test_glued_query_splits():
    rows = [_row("Sony A7R IV Body"), _row("Sony A7 IV Body")]
    assert [r["name"] for r in sc.rerank("sony a7iv", rows)] == ["Sony A7 IV Body"]


def test_glued_title_matches_spaced_query():
    assert [r["name"] for r in sc.rerank("sony a7 iv", [_row("Sony A7IV Camera")])] \
        == ["Sony A7IV Camera"]


def test_bare_letter_suffix_not_split():
    rows = [_row("Sony A7 IV Body"), _row("Sony A7R IV Body")]
    assert [r["name"] for r in sc.rerank("sony a7r iv", rows)] == ["Sony A7R IV Body"]


def test_descriptor_typo_still_matches():
    assert [r["name"] for r in sc.rerank("sonny a7 iv", [_row("Sony A7 IV Camera")])] \
        == ["Sony A7 IV Camera"]


def test_descriptor_absence_does_not_reject():
    assert [r["name"] for r in sc.rerank("sony a7 iv", [_row("A7 IV Camera")])] \
        == ["A7 IV Camera"]


def test_pure_integer_not_a_gate():
    rows = [_row("Peak Design Travel Tripod")]
    assert [r["name"] for r in sc.rerank("travel tripod under 400", rows)] \
        == ["Peak Design Travel Tripod"]


def test_empty_query_and_rows():
    assert sc.rerank("", [_row("Sony A7 IV")]) == []
    assert sc.rerank("sony", []) == []


def test_tie_break_price_then_url():
    rows = [_row("Sony A7 IV", "1998.00", "https://x/b"),
            _row("Sony A7 IV", "1899.00", "https://x/a"),
            _row("Sony A7 IV", "1899.00", "https://x/z")]
    assert [r["url"] for r in sc.rerank("sony a7 iv", rows)] == \
        ["https://x/a", "https://x/z", "https://x/b"]


def test_relevance_export():
    assert sc.relevance("sony a7 iv", "Sony A7 IV Camera") >= 0.0
    assert sc.relevance("sony a7 iv", "Sony A7R IV Camera") < 0.0
    assert sc.relevance("sony a7 iv", "Sony A7 III Camera") < 0.0
