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

    # SETTLED vs ADVISORY (generalised 2026-08-25 so a new vertical stops jamming
    # the bank). body/lens/support are the regime the eBay id AUTHORITATIVELY
    # derives, so a genuine disagreement THERE is a hard drift (the 30093->30097
    # catch this guard exists for). A vertical facet the map does not own
    # (action_cam, and future gimbal/drone/...) or an unmapped id is NOT a drift:
    # its facet comes from the vertical pipeline and the eBay id is at most
    # advisory. Those become a LOUD, NON-BREAKING notice with the exact fix line,
    # so expanding into a new product category is a review nudge, not a wall.
    contradictions, notices, reached = [], [], 0
    for slug, e in entries.items():
        facet = skus_registry.get_facet(e) or ''
        cid = skus_registry.get_marketplace_category(e, 'ebay_category_id') or ''
        if not cid:
            continue
        reached += 1
        mapped = ecm.category_for(cid)
        # The registry's coarse facet ('lens' for the Sigma, not 'lens/prime')
        # is exactly what the map emits — sub-typing is a card-BUILD concern.
        verdict = ecm.reconcile(facet, cid)
        if verdict == 'ok':
            continue
        elif verdict == 'drift':
            contradictions.append((slug, cid, facet, mapped))
        else:  # 'notice' — unmapped id or vertical facet the map does not own
            notices.append((slug, cid, facet, mapped))

    if notices:
        lines = "\n".join(
            f"   {slug}: ebay {cid!r} facet {facet!r} "
            f"(map -> {mapped or 'UNKNOWN'!r}) — add ecm._CATEGORY_MAP[{cid!r}]="
            f"{facet!r} if the eBay id is right, else fix the SKU's id"
            for slug, cid, facet, mapped in notices)
        print(
            f"\n[ebay_category_map] {len(notices)} NON-BREAKING coverage "
            f"notice(s) — a new vertical/leaf the map does not yet own:\n{lines}")

    assert not contradictions, (
        "seed table drifted from ground truth (both sides are settled "
        "body/lens/support and disagree):\n" + "\n".join(
            f"   {slug}: map gives {mapped!r} for ebay id {cid!r} but registry "
            f"says {facet!r}" for slug, cid, facet, mapped in contradictions))

    # THE TRIPWIRE. A guard that can be disarmed by a field rename and still
    # report success is not a guard. If the registry has entries but NONE reached
    # the read (no cid ever resolved), the read path is broken, not the data —
    # fail loudly rather than pass vacuously. (notices/contradictions count as
    # reached; a vertical-only registry is legitimately all-notices.)
    assert not (entries and reached == 0), (
        f"read path is dead: {len(entries)} registry entries, 0 reached the "
        f"category read. A field has moved and the accessors did not follow — "
        f"fix the read, do not delete this tripwire.")


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


def test_action_cam_leaf_maps_and_is_controlled():
    # 11724 (Camcorders) is where action cams live on eBay.
    assert ecm.category_for('11724') == 'action_cam'
    assert 'action_cam' in ecm.CONTROLLED_CATEGORIES
    # ...but action_cam is NOT part of the settled, hard-guarded regime.
    assert 'action_cam' not in ecm.SETTLED_FACETS


def test_reconcile_settled_agreement_and_drift():
    # Agreement within the settled regime.
    assert ecm.reconcile('lens', '3323') == 'ok'
    assert ecm.reconcile('support', '30093') == 'ok'
    # A genuine settled-vs-settled disagreement is the hard drift (a lens leaf on
    # a body facet) — this is the class the guard must keep failing on.
    assert ecm.reconcile('body', '3323') == 'drift'


def test_reconcile_vertical_and_unmapped_are_non_breaking_notices():
    # A vertical facet the map does not own is a notice, never a drift, even when
    # the id maps to a settled bucket (gopro-hero10's generic 31388 -> 'body').
    assert ecm.reconcile('action_cam', '31388') == 'notice'
    # The precise camcorder leaf agrees outright.
    assert ecm.reconcile('action_cam', '11724') == 'ok'
    # An unmapped id abstains -> notice, for a settled OR vertical facet.
    assert ecm.reconcile('action_cam', '99999999') == 'notice'
    assert ecm.reconcile('body', '99999999') == 'notice'
    # A future vertical bucket (not yet in the map at all) is still a notice.
    assert ecm.reconcile('gimbal', '31388') == 'notice'
