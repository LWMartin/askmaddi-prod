"""Tests for ebay_category_map — the mint-path category derivation."""
import json
from pathlib import Path

import ebay_category_map as ecm


def test_frozen_registry_ids_map_to_their_human_category():
    """The four frozen skus.json entries are a hand-verified labeled table: each
    pairs an ebay_category_id with the human-assigned category. The map MUST
    agree with them, because they are the ground truth it was seeded from — a
    drift here means the map contradicts the curated registry."""
    skus = Path(__file__).parent.parent / 'data' / 'skus.json'
    if not skus.exists():
        import pytest
        pytest.skip("skus.json not present")
    reg = json.loads(skus.read_text())
    for slug, e in (reg.get('skus') or {}).items():
        cat = e.get('category', '')
        cid = e.get('identity', {}).get('ebay_category_id', '')
        if not cid:
            continue
        mapped = ecm.category_for(cid)
        # The registry's coarse category ('lens' for the Sigma, not 'lens/prime')
        # is exactly what the map emits.
        assert mapped == cat, (
            f"{slug}: map gives {mapped!r} for ebay id {cid!r} but registry "
            f"says {cat!r} — the seed table drifted from ground truth")


def test_known_ids_map_to_controlled_buckets():
    assert ecm.category_for('88433') == 'body'
    assert ecm.category_for('3323') == 'lens'
    assert ecm.category_for('30093') == 'support'
    for cid in ('88433', '3323', '30093'):
        assert ecm.category_for(cid) in ecm.CONTROLLED_CATEGORIES


def test_unknown_id_returns_empty_not_a_guess():
    # The safety seam: an unmapped id abstains (review signal), never guesses.
    assert ecm.category_for('99999999') == ''
    assert ecm.is_known('99999999') is False


def test_blank_and_none_are_unknown():
    assert ecm.category_for('') == ''
    assert ecm.category_for(None) == ''
    assert ecm.category_for('   ') == ''


def test_int_id_is_coerced():
    # getItem returns categoryId as a string, but be tolerant of an int caller.
    assert ecm.category_for(88433) == 'body'


def test_is_known_matches_category_for():
    assert ecm.is_known('3323') is True
    assert ecm.is_known('') is False
