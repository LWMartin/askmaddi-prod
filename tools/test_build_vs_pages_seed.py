"""Tests for curated seed-driven vs-page selection (build_vs_pages.py).

Which pairs get built is a ratified human judgment held in data/vs_pairs.json,
not an on-card heuristic. These lock: seed loading, seed->pair resolution with
loud skips for missing/thin pairs, canonical+dedup output, build_pages modes,
and that the shipped seed resolves cleanly against the live cards.
"""
import json
from pathlib import Path

import build_vs_pages as V


def _card(card_id, name, lead, detail=None):
    """Minimal card with visible axes carrying sentiment (so shared_axes works)."""
    def axis(aid, pos, neu, neg):
        return {"axis_id": aid, "display_name": aid.title(),
                "sentiment": {"pos": pos, "neu": neu, "neg": neg,
                              "total": pos + neu + neg}}
    return {
        "card_id": card_id,
        "identity": {"display_name": name, "brand": "TestCo",
                     "category": "body", "subcategory": "mirrorless"},
        "pricing": {"msrp_usd": 1000, "used_market": {"bands": {"pre_owned": 800}}},
        "lead_axes": [axis(*a) for a in lead],
        "detail_axes": [axis(*a) for a in (detail or [])],
    }


CARD_A = _card("aa", "Aye", [("autofocus", 5, 2, 1), ("handling", 4, 3, 1)])
CARD_B = _card("bb", "Bee", [("autofocus", 3, 3, 2), ("handling", 6, 1, 1)])
CARD_C = _card("cc", "Cee", [("autofocus", 2, 2, 2)])  # shares only 1 axis w/ A


# ─── load_seed ───────────────────────────────────────────────────────────────

def test_load_seed_parses_pairs(tmp_path):
    p = tmp_path / "seed.json"
    p.write_text(json.dumps({"pairs": [{"a": "aa", "b": "bb"},
                                       {"a": "cc", "b": "dd"}]}), encoding="utf-8")
    assert V.load_seed(p) == [("aa", "bb"), ("cc", "dd")]


def test_load_seed_missing_or_malformed_returns_empty(tmp_path):
    assert V.load_seed(tmp_path / "nope.json") == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert V.load_seed(bad) == []


def test_load_seed_skips_unbuilt_statuses(tmp_path):
    # 'live'/unset build; 'proposed'/'hold' stay dark until a human ratifies.
    p = tmp_path / "seed.json"
    p.write_text(json.dumps({"pairs": [
        {"a": "aa", "b": "bb", "status": "live"},
        {"a": "cc", "b": "dd"},                       # unset -> builds
        {"a": "ee", "b": "ff", "status": "proposed"},  # comparator_fork -> dark
        {"a": "gg", "b": "hh", "status": "hold"},
    ]}), encoding="utf-8")
    assert V.load_seed(p) == [("aa", "bb"), ("cc", "dd")]


def test_load_seed_drops_incomplete_and_self_pairs(tmp_path):
    p = tmp_path / "seed.json"
    p.write_text(json.dumps({"pairs": [{"a": "aa"}, {"a": "xx", "b": "xx"},
                                       {"a": "aa", "b": "bb"}]}), encoding="utf-8")
    assert V.load_seed(p) == [("aa", "bb")]


# ─── select_seeded_pairs ─────────────────────────────────────────────────────

def test_select_seeded_pairs_keeps_valid_pair():
    pairs, skipped = V.select_seeded_pairs([CARD_A, CARD_B], [("aa", "bb")])
    assert len(pairs) == 1
    assert V.vs_slug(*pairs[0]) == "aa-vs-bb"
    assert skipped == []


def test_select_seeded_pairs_skips_missing_card_loudly():
    pairs, skipped = V.select_seeded_pairs([CARD_A], [("aa", "zz")])
    assert pairs == []
    assert len(skipped) == 1 and "missing" in skipped[0][2] and "zz" in skipped[0][2]


def test_select_seeded_pairs_skips_thin_overlap():
    # A and C share only 1 axis; min_shared=2 must reject it, with a reason.
    pairs, skipped = V.select_seeded_pairs([CARD_A, CARD_C], [("aa", "cc")], min_shared=2)
    assert pairs == []
    assert "shared" in skipped[0][2]


def test_select_seeded_pairs_canonical_and_deduped():
    # b-vs-a and a-vs-b collapse to one canonical slug.
    pairs, _ = V.select_seeded_pairs([CARD_A, CARD_B], [("bb", "aa"), ("aa", "bb")])
    assert len(pairs) == 1
    assert V.vs_slug(*pairs[0]) == "aa-vs-bb"


# ─── build_pages orchestration ───────────────────────────────────────────────

def test_build_pages_curated_writes_seed_pages(tmp_path):
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"pairs": [{"a": "aa", "b": "bb"}]}), encoding="utf-8")
    slugs, skipped, mode = V.build_pages([CARD_A, CARD_B], tmp_path / "vs", seed_path=seed)
    assert mode == "curated"
    assert slugs == ["aa-vs-bb"]
    assert (tmp_path / "vs" / "aa-vs-bb" / "index.html").exists()


def test_build_pages_no_seed_writes_nothing_without_auto(tmp_path):
    slugs, _skipped, mode = V.build_pages(
        [CARD_A, CARD_B], tmp_path / "vs", seed_path=tmp_path / "none.json")
    assert mode == "none" and slugs == []
    assert not (tmp_path / "vs").exists()


def test_build_pages_auto_fallback_when_no_seed(tmp_path):
    slugs, _skipped, mode = V.build_pages(
        [CARD_A, CARD_B], tmp_path / "vs",
        seed_path=tmp_path / "none.json", fallback_auto=True)
    assert mode == "auto"
    assert slugs == ["aa-vs-bb"]


# ─── the shipped seed resolves against live cards ────────────────────────────

def test_shipped_seed_resolves_cleanly_against_live_cards():
    """Guardrail: every pair in the committed data/vs_pairs.json must reference
    real cards and clear the shared-axis floor — a stale seed entry fails here
    loudly rather than silently dropping a page in production."""
    repo = Path(V.__file__).resolve().parent.parent
    cards_dir = repo / "data" / "cards"
    if not cards_dir.is_dir():
        return  # not in a full checkout; skip
    cards = V._load_cards(cards_dir)
    seed = V.load_seed(repo / "data" / "vs_pairs.json")
    assert seed, "shipped seed should be non-empty"
    pairs, skipped = V.select_seeded_pairs(cards, seed, min_shared=1)
    assert skipped == [], f"stale seed entries: {skipped}"
    assert len(pairs) == len(seed)
