"""Tests for ebay_category_map — the mint-path category derivation."""
import json
from pathlib import Path

import ebay_category_map as ecm
import skus_registry


def test_frozen_registry_ids_map_to_their_human_category():
    """Every registry entry is a hand-verified labeled pair: an ebay_category_id
    beside the human-assigned facet. The map MUST agree with all of them,
    because they are the ground truth it was seeded from — a drift here means
    the map contradicts the curated registry.

    READ THROUGH THE ACCESSORS, NEVER BY HAND. This guard previously reached
    into `entry['category']` and `entry['identity']['ebay_category_id']`
    directly. The substrate migration (tools/migrate_substrate.py) renamed
    `category` -> `facet` and hoisted the leaf id into `marketplace_categories`,
    so both hand-rolled paths went dead — `cid` came back empty on every entry,
    the loop `continue`d past all 14, and the test stayed GREEN while asserting
    NOTHING. The 30093 -> 30097 leaf drift on ulanzi-f38-zero (2026-07-09) then
    rode undetected for three weeks under a guard built to catch exactly it.

    `skus_registry.get_facet` / `get_marketplace_category` both carry old-shape
    fallbacks, which is why THEY survived the migration when this test did not.
    Reading through them is the structural fix: the field can move again and the
    guard follows it.
    """
    skus = Path(__file__).parent.parent / 'data' / 'skus.json'
    if not skus.exists():
        import pytest
        pytest.skip("skus.json not present")
    reg = json.loads(skus.read_text())
    entries = (reg.get('skus') or {})

    checked = 0
    for slug, e in entries.items():
        facet = skus_registry.get_facet(e) or ''
        cid = skus_registry.get_marketplace_category(e, 'ebay_category_id') or ''
        if not cid:
            continue
        checked += 1
        mapped = ecm.category_for(cid)
        # The registry's coarse facet ('lens' for the Sigma, not 'lens/prime')
        # is exactly what the map emits — sub-typing is a card-BUILD concern.
        assert mapped == facet, (
            f"{slug}: map gives {mapped!r} for ebay id {cid!r} but registry "
            f"says {facet!r} — the seed table drifted from ground truth")

    # THE TRIPWIRE. A guard that can be disarmed by a field rename and still
    # report success is not a guard. If the registry has entries but none of
    # them reached an assertion, the read path is broken, not the data — fail
    # loudly rather than pass vacuously, which is the failure mode this whole
    # test just spent three weeks demonstrating.
    assert not (entries and checked == 0), (
        f"read path is dead: {len(entries)} registry entries, 0 reached an "
        f"assertion. A field has moved and the accessors did not follow — fix "
        f"the read, do not delete this tripwire.")


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
