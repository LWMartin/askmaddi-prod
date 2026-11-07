"""Tests for pin_identity — the gate that a new-price pin is the card's own product.

The known cases and `test_real_pins_pass` use LITERAL URLs and models copied from
real artifacts (askmaddi-prod card JSONs at a978d507 / e92ea7d1). The adversarial
and plumbing tests use CONSTRUCTED Adorama-shaped URLs, labelled as such. No test
reads live data/ — data/cards churns nightly (the 2026-11-06 lesson; see
test_build_site_guide_prune.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pin_identity import card_model, check_pin, destination, units  # noqa: E402

A = "https://www.adorama.com/"
WRAP = "https://adorama.prf.hn/click/camref:1101l5Pw9q/creativeref:1110l15401/destination:"

R6_MARK_II = A + "canon-eos-r6-mark-ii-mirrorless-camera-body-only/p/car6m2"
T7_USED = A + ("used-canon-eos-2000d-digital-camera-body-rebel-t7-with-canon-ef-s-"
               "18-55mm-f-35-56-is-ii-lens/p/us2087889")
GH5_II = A + "panasonic-lumix-gh5ii-mirrorless-camera-body-dc-gh5ii-4k-6k/p/ipcdcgh5m2"
R8 = A + "canon-eos-r8-mirrorless-camera-body-full-frame-24mp/p/car8"
SONY_70200_GM2 = A + "sony-fe-70-200-mm-f-2-8-gm-oss-ii-lens/p/iso70200gm2"


def _wrapped(dest):
    from urllib.parse import quote
    return WRAP + quote(dest, safe="")


# ─── the known cases: every one of them must be refused ──────────────────────

def test_canon_r6_false_price_2026_11_04_is_refused():
    """Live 11-04..11-05: card 'R6' showed the R6 Mark II at $1999."""
    why = check_pin("R6", _wrapped(R6_MARK_II))
    assert why == ["generation: product is II, card is not"]


def test_same_product_is_right_for_the_r6_ii_card():
    """The SAME URL is the correct pin for the 'eos r6 ii' card ("Mark" is filler)."""
    assert check_pin("eos r6 ii", _wrapped(R6_MARK_II)) == []


def test_canon_550d_used_t7_kit_is_refused_three_ways():
    """Live 11-04..11-05: card 'eos 550d' showed a USED 2000D/T7 kit at $420,
    labelled condition "N" by the feed — so the check never trusts that flag."""
    why = check_pin("eos 550d", _wrapped(T7_USED))
    assert why[0].startswith("used:")
    assert any(w == "model: 550d not in pinned product" for w in why)
    assert any(w.startswith("generation:") for w in why)


def test_gh5_priced_as_gh5_ii_is_refused():
    """Found by this gate 2026-11-06: matcher MPN prefix 'dcgh5' ⊂ 'dcgh5m2'."""
    assert check_pin("gh5", GH5_II) == ["generation: product is II, card is not"]


def test_r8_ii_card_priced_as_r8_is_refused():
    """Found by this gate 2026-11-06: card 'eos r8 ii', pin is the original R8."""
    assert check_pin("eos r8 ii", R8) == ["generation: card is II, product is not"]


def test_card_name_that_omits_its_generation_is_refused():
    """sony-70-200mm-f2-8: spine MPN SEL70200GM2 says GM II, the card name does not.
    The pin may be right, but the card under-specifies — fixed in data, never by
    loosening the check (11-05 ruling: wrong identity is fixed in data)."""
    assert check_pin("70 200mm f2.8", SONY_70200_GM2) == ["generation: product is II, card is not"]
    assert check_pin("70-200mm f2.8 GM II", SONY_70200_GM2) == []


# ─── real pins that are right must pass (one per rule path) ──────────────────

def test_real_pins_pass():
    for model, url in [
        ("A7 IV (ILCE-7M4)",  # parenthetical part code is an alias, not required
         A + "sony-alpha-a7-iv-mirrorless-camera-body-34mp-full-frame-4k/p/isoa7m4"),
        ("A1", A + "sony-alpha-1-mirrorless-camera/p/isoa1"),            # alpha == a
        ("e m10 iv",  # "Mark IV" == "IV"
         A + "olympus-om-d-e-m10-mark-iv-mirrorless-camera-body-only/p/iomem104b"),
        ("16 28mm f2.8", A + "canon-rf-16-28-mm-f-2-8-is-stm-lens/p/car1628is"),  # decimals
        ("z5", A + "nikon-z5-full-frame-mirrorless-camera-body-only/p/nkz5"),
    ]:
        assert check_pin(model, _wrapped(url)) == [], model


def test_constructed_f_slash_model_with_generation_passes():
    """CONSTRUCTED url; the model is real (card sigma-35-art-dg-dn-ii, unpinned)."""
    assert check_pin("35mm f/1.4 DG DN Art II",
                     A + "sigma-35-mm-f-1-4-dg-dn-art-ii-lens/p/sg3514dn2") == []


# ─── adversarial: generation swaps fused into the model code ─────────────────

def test_fused_generation_swaps_are_refused():
    cases = [
        ("x100v", A + "fujifilm-x100vi-digital-camera-silver/p/ifjx100vis"),   # v -> vi
        ("r5", A + "canon-eos-r50-mirrorless-camera-body/p/car50"),            # r5 -> r50
        ("z5", A + "nikon-z50-mirrorless-camera-body/p/nkz50"),                # z5 -> z50
        ("A7 IV", A + "sony-alpha-a7-iii-mirrorless-camera-body/p/isoa7m3"),   # iv -> iii
        ("zv-e10", A + "sony-zv-e10-ii-mirrorless-camera-body/p/isozve10m2"),  # -> ii
        ("eos r6 ii", A + "canon-eos-r6-mirrorless-camera-body/p/car6"),       # ii -> none
        ("50mm f1.8", A + "canon-rf-50-mm-f-1-4-stm-lens/p/car5014"),          # f1.8 -> f1.4
    ]
    for model, url in cases:
        assert check_pin(model, url), f"{model} must not accept {url}"


def test_used_refurb_open_box_are_refused_by_slug_title_or_code():
    assert check_pin("z5", A + "refurbished-nikon-z5-camera-body/p/nkz5r")[0].startswith("used:")
    assert check_pin("z5", A + "nikon-z5-open-box/p/nkz5ob")[0].startswith("used:")
    assert check_pin("z5", A + "nikon-z5-body/p/us1234567")[0].startswith("used:")
    assert check_pin("z5", A + "nikon-z5-body/p/nkz5", title="Nikon Z5 (Used, Excellent)")[0] \
        .startswith("used:")
    # 'usm' (Canon Ultrasonic Motor) is not 'used'
    assert check_pin("ef 70 200mm f2.8",
                     A + "canon-ef-70-200mm-f-2-8l-is-usm-lens/p/ca70200usm") == []


def test_title_can_supply_atoms_but_not_contradict_slug_generation():
    url = A + "panasonic-mirrorless-camera/p/x"
    assert check_pin("gh5", url, title="Panasonic Lumix GH5 Body") == []
    assert check_pin("gh5", GH5_II, title="Panasonic Lumix GH5 Body") \
        == ["generation: product is II, card is not"]


# ─── plumbing ────────────────────────────────────────────────────────────────

def test_destination_unwraps_prf_link():
    assert destination(_wrapped(R6_MARK_II)) == ("canon-eos-r6-mark-ii-mirrorless-camera-body-only",
                                                 "car6m2")
    assert destination(None) == ("", "")


def test_units_split_runs_drop_filler_fold_alpha():
    assert units("Sony Alpha A7 Mark IV f/2.8") == ["sony", "a", "a", "7", "iv", "f", "2", "8"]


def test_card_model_prefers_model_then_display_name_never_mpn():
    assert card_model({"identity": {"model": "R6", "display_name": "Canon R6",
                                    "mpn": "Canon EOS R6 Mark III"}}) == "R6"
    assert card_model({"identity": {"display_name": "Canon EOS R5"}}) == "Canon EOS R5"
    assert card_model({}) == ""
    assert check_pin("", R8) == ["model: card has no identity.model to check against"]
