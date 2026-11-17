"""Tests for tools/product_aliases.py (2026-11-17).

The anchor is the real case: Search Console showed the a7 IV page searched as
"a74", "a7 4", "a7 m4", "a7 iv". Derived from the card's own name + spine MPN
(pinned below as literals — tests never read pipeline-owned data), the page
must carry every one of them. Then the guards: a wrong MPN yields no part-number
form, lenses get no numeral forms, collisions with another card are dropped."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from product_aliases import (catalog_aliases, derive_aliases, key,  # noqa: E402
                     visible_aliases)

# Real identity, data/cards/sony-a7iv.json + data/skus.json spine MPN, 2026-11-17.
A7IV = {"card_id": "sony-a7iv",
        "identity": {"display_name": "Sony A7 IV (ILCE-7M4)", "brand": "Sony",
                     "category": "body",
                     "sku_alt_names": ["Sony a7 IV", "Sony A7IV", "ILCE-7M4"]}}
A7IV_MPN = "ILCE-7M4"
GSC_QUERIES_2026_11_17 = ["a74", "a7 4", "a7 m4", "a7 iv"]


def _a7iv():
    i = A7IV["identity"]
    return derive_aliases(i["display_name"], i["brand"], A7IV_MPN, i["category"],
                          i["sku_alt_names"])


def test_every_real_search_spelling_is_carried():
    carried = {key(a) for a in _a7iv()} | {key("A7 IV")}
    missing = [q for q in GSC_QUERIES_2026_11_17 if key(q) not in carried]
    assert missing == []


def test_a7iv_exact_alias_list():
    assert _a7iv() == ["ILCE-7M4", "A7IV", "A7 4", "A74", "A7 M4", "A7M4", "a7 IV"]


def test_visible_line_is_one_readable_spelling_per_name_not_in_title():
    vis = visible_aliases("Sony A7 IV (ILCE-7M4)", _a7iv(), "Sony")
    assert vis == ["A7 4", "A7 M4"]


def test_wrong_mpn_yields_no_part_number_form():
    # Real: sony-a7-v's spine carries ILCE7RM5B — the A7R V's part number.
    al = derive_aliases("Sony A7 V", "Sony", "ILCE7RM5B", "body")
    assert not any("M5" in a for a in al)
    assert al == ["A7V", "A7 5", "A75"]


def test_mpn_digits_must_start_the_model_run():
    # RX10 must not borrow the RX100's part number.
    assert not any("M5" in a for a in derive_aliases("Sony RX10 V", "Sony", "DSC-RX100M5", "body"))
    assert "RX10 M5" in derive_aliases("Sony RX10 V", "Sony", "DSC-RX10M5", "body")
    assert "A7S M3" in derive_aliases("Sony A7S III", "Sony", "ILCE7SM3", "body")
    assert "A7R M2" in derive_aliases("Sony A7R II", "Sony", "ILCE-7RM2/B", "body")


def test_mpn_generation_must_match_card_generation():
    assert not any("M3" in a for a in derive_aliases("Sony A7 IV", "Sony", "ILCE-7M3", "body"))


def test_mark_is_filler():
    al = derive_aliases("Canon EOS R6 Mark III", "Canon", "7084C002", "body")
    assert al[:2] == ["EOS R6 Mark 3", "EOS R6 III"]
    assert "EOS R6 3" in al


def test_lens_trailing_numeral_is_not_a_generation():
    assert derive_aliases("Sony FE 70-200mm f/2.8 GM OSS II", "Sony", "SEL70200GM2", "lens") == []


def test_no_numeral_no_mpn_no_aliases():
    assert derive_aliases("Sony A7", "Sony", "ILCE-7", "body") == []


def test_collision_with_another_card_drops_alias_from_both():
    a = {"card_id": "x-a7-4", "identity": {"display_name": "Acme A7 IV", "brand": "Acme",
                                           "category": "body"}}
    b = {"card_id": "x-a74", "identity": {"display_name": "Acme A74", "brand": "Acme",
                                          "category": "body"}}
    out = catalog_aliases([a, b])
    assert "A74" not in out["x-a7-4"] and "A7 4" not in out["x-a7-4"]
    assert "A7IV" in out["x-a7-4"]


def test_same_alias_different_brand_is_not_a_collision():
    a = {"card_id": "s", "identity": {"display_name": "Sony A7 IV", "brand": "Sony", "category": "body"}}
    b = {"card_id": "z", "identity": {"display_name": "Zeta A7 IV", "brand": "Zeta", "category": "body"}}
    out = catalog_aliases([a, b])
    assert "A7 4" in out["s"] and "A7 4" in out["z"]


def test_mpn_match_is_anchored_at_model_start():
    # Real pair: the RX0 II (DSC-RX0M2) must not read the RX10 II's part
    # number (DSC-RX10M2) as its own — "0M2" sits inside "10M2".
    assert not any("M2" in a for a in derive_aliases("Sony RX0 II", "Sony", "DSC-RX10M2", "body"))
    assert "RX0 M2" in derive_aliases("Sony RX0 II", "Sony", "DSC-RX0M2", "body")


def test_mpn_generation_digit_is_whole():
    assert not any("M4" in a for a in derive_aliases("Sony A7 IV", "Sony", "ILCE-7M45", "body"))


def test_alias_vs_alias_collision_drops_from_both():
    a = {"card_id": "p", "identity": {"display_name": "Acme A7 IV", "brand": "Acme", "category": "body"}}
    b = {"card_id": "q", "identity": {"display_name": "Acme A7 Mark IV", "brand": "Acme", "category": "body"}}
    out = catalog_aliases([a, b])
    assert "A7 4" not in out["p"] and "A7 4" not in out["q"]
