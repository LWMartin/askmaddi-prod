"""Tests for the advisory near-duplicate detector.

The load-bearing case is the mint-drift dup that motivated it: canon-eos-r6-v
(model "eos r6 v") IS the already-carded canon-r6, but a different slug, so the
exact-collision gate missed it. It must surface here — while genuinely distinct
same-brand products (R6 vs R5, A7 V vs A7S III) must NOT.
"""
import dedup_suspects as ds


# The live 12-card universe shape (slug, brand, model).
CARDS = [
    ("canon-r6", "Canon", "R6"),
    ("canon-r5", "Canon", "R5"),
    ("sony-a7-v", "Sony", "A7 V"),
    ("sony-a7s-iii", "Sony", "A7S III"),
    ("sigma-35mm-f1-2-dg-dn-art", "Sigma", "35mm F1.2 DG DN Art"),
    ("peak-design-travel-tripod", "Peak Design", "Travel Tripod"),
    ("peak-design-pro-tripod", "Peak Design", "Pro Tripod"),
]


def test_mint_drift_dup_is_flagged():
    """canon-eos-r6-v ('eos r6 v') must surface canon-r6 — the motivating case."""
    hits = ds.suspects("canon-eos-r6-v", "Canon", "eos r6 v", CARDS)
    assert hits, "the mint-drift dup of canon-r6 was not surfaced"
    assert hits[0][0] == "canon-r6"


def test_distinct_same_brand_bodies_not_flagged():
    """R6 vs R5 are different products — an R6 build must not flag R5."""
    hits = ds.suspects("canon-r6", "Canon", "R6", CARDS)
    # canon-r6 itself is excluded (self); nothing else Canon overlaps.
    assert all(s != "canon-r5" for s, _ in hits)
    assert hits == []


def test_cross_brand_token_clash_ignored():
    """A Sigma '35mm' lens must not flag a hypothetical other-brand 35mm — the
    same-brand guard kills coincidental token overlap."""
    others = CARDS + [("tamron-35mm", "Tamron", "35mm F1.4")]
    hits = ds.suspects("sigma-35mm-f1-2-dg-dn-art", "Sigma",
                       "35mm F1.2 DG DN Art", others)
    assert all(b_slug != "tamron-35mm" for b_slug, _ in hits)


def test_self_slug_excluded():
    hits = ds.suspects("sony-a7-v", "Sony", "A7 V", CARDS)
    assert all(s != "sony-a7-v" for s, _ in hits)


def test_empty_model_returns_nothing():
    assert ds.suspects("mystery", "Canon", "", CARDS) == []
    assert ds.suspects("mystery", "Canon", "eos", CARDS) == []  # all-filler


def test_a7v_not_confused_with_a7siii():
    """A7 V vs A7S III — same brand, but the model cores diverge; no flag."""
    hits = ds.suspects("sony-a7-v", "Sony", "A7 V", CARDS)
    assert all(s != "sony-a7s-iii" for s, _ in hits)


def test_score_is_ordered_and_bounded():
    others = CARDS + [("canon-r6-ii", "Canon", "R6 II")]
    hits = ds.suspects("canon-eos-r6-v", "Canon", "eos r6 v", others)
    scores = [sc for _, sc in hits]
    assert scores == sorted(scores, reverse=True)
    assert all(0.0 < sc <= 1.0 for sc in scores)
