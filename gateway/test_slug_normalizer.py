#!/usr/bin/env python3
"""Tests for slug_normalizer — the frozen-slug invariant and its seams.

No network. Uses a tmp skus.json as the override table so the cadre's real
frozen slugs are exercised without coupling to the live data/skus.json churn.

Graduated into askmaddi-prod 2026-06-24. Imports the in-repo slug_normalizer
(which imports slug_common); identical behavior to the phantom-ops original.
"""
import json
import sys
from pathlib import Path

import pytest

# gateway/ holds slug_normalizer + slug_common; ensure it is importable whether
# pytest is run from repo root or from gateway/.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import slug_normalizer as sn


# The real seed cadre, as frozen on 2026-06-23. These slugs are FACTS.
CADRE = {
    "version": "0.1.0",
    "skus": {
        "peak-design-pro-tripod": {"vendor": "Peak Design", "model": "Pro Tripod"},
        "peak-design-travel-tripod": {"vendor": "Peak Design", "model": "Travel Tripod"},
        "sigma-35-art-dg-dn-ii": {"vendor": "Sigma", "model": "35mm f/1.4 DG DN Art II"},
        "sony-a7iv": {"vendor": "Sony", "model": "A7 IV (ILCE-7M4)"},
    },
}


@pytest.fixture
def skus(tmp_path) -> Path:
    p = tmp_path / "skus.json"
    p.write_text(json.dumps(CADRE))
    return p


# ─── frozen invariant: cadre slugs are read, never regenerated ──────────────

def test_explicit_override_returned_verbatim(skus):
    # A hand-authored slug that the generator would never produce.
    res = sn.resolve_slug("Sigma", "35mm f/1.4 DG DN Art II",
                          override="sigma-35-art-dg-dn-ii", skus_path=skus)
    assert res.slug == "sigma-35-art-dg-dn-ii"
    assert res.source == "override"
    assert res.needs_review is False


def test_generated_that_matches_frozen_is_read_not_reminted(skus):
    # Peak Design Pro Tripod: the mechanical slug HAPPENS to equal the frozen
    # one. It must come back as 'override' (a fact), not 'generated'.
    res = sn.resolve_slug("Peak Design", "Pro Tripod", skus_path=skus)
    assert res.slug == "peak-design-pro-tripod"
    assert res.source == "override"
    assert res.needs_review is False


def test_hand_authored_cadre_resolved_by_identity_not_reminted(skus):
    # The cadre-protecting case the live run exposed: Sony's mechanical slug
    # ('sony-a7-iv-ilce-7m4') does NOT equal the frozen 'sony-a7iv'. Feeding
    # the existing card's vendor/model (e.g. a Stage-6 rebuild) MUST resolve to
    # the frozen slug via identity, never mint a fracturing fresh one.
    res = sn.resolve_slug("Sony", "A7 IV (ILCE-7M4)", skus_path=skus)
    assert res.slug == "sony-a7iv"
    assert res.source == "override"
    assert res.needs_review is False

    res2 = sn.resolve_slug("Sigma", "35mm f/1.4 DG DN Art II", skus_path=skus)
    assert res2.slug == "sigma-35-art-dg-dn-ii"
    assert res2.source == "override"
    assert res2.needs_review is False


# ─── the spec's cautionary case: rule can't match human taste ───────────────

def test_sigma_generated_differs_from_hand_authored(skus):
    # If the Sigma card did NOT already exist, the generator would propose the
    # mechanical slug — provably different from the hand-authored cadre slug.
    empty = skus.parent / "empty.json"
    empty.write_text(json.dumps({"version": "0", "skus": {}}))
    res = sn.resolve_slug("Sigma", "35mm f/1.4 DG DN Art II", skus_path=empty)
    assert res.source == "generated"
    assert res.needs_review is True
    assert res.slug != "sigma-35-art-dg-dn-ii"   # the whole reason overrides exist


# ─── genuinely new SKU: generated proposal, flagged for review ──────────────

def test_new_sku_is_generated_and_flagged(skus):
    res = sn.resolve_slug("Tamron", "28-75mm f/2.8 G2", skus_path=skus)
    assert res.source == "generated"
    assert res.needs_review is True
    assert res.slug == "tamron-28-75mm-f-2-8-g2"  # frozen slugify rule, unchanged


# ─── collision detection: sony-a7iv ~ sony-a7-iv class ──────────────────────

def test_override_colliding_with_frozen_is_surfaced(skus):
    # A human proposes 'sony-a7-iv' (hyphenated) while 'sony-a7iv' is frozen.
    # Same product under normalization → must be flagged, not silently accepted.
    res = sn.resolve_slug("Sony", "A7 IV", override="sony-a7-iv", skus_path=skus)
    assert res.slug == "sony-a7-iv"          # the human's choice is honored
    assert res.collision == "sony-a7iv"      # but the clash is loud


def test_no_false_collision_for_distinct_slug(skus):
    res = sn.resolve_slug("Nikon", "Z6 III", override="nikon-z6-iii", skus_path=skus)
    assert res.collision is None


# ─── robustness: missing/alt-shape registry ─────────────────────────────────

def test_missing_skus_file_means_empty_override_table(tmp_path):
    res = sn.resolve_slug("Peak Design", "Pro Tripod",
                          skus_path=tmp_path / "nope.json")
    # Nothing frozen → even the cadre name is a generated proposal.
    assert res.source == "generated"
    assert res.needs_review is True


def test_list_shape_registry_is_read(tmp_path):
    p = tmp_path / "skus.json"
    p.write_text(json.dumps([{"sku_id": "sony-a7iv"}]))
    res = sn.resolve_slug("Sony", "A7 IV", override="sony-a7iv", skus_path=p)
    assert res.source == "override"
