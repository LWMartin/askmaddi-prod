"""Tests for spine_canonicalize — the aerial-vertical model de-contaminator."""
import spine_canonicalize as sc


# ── canonicalize_model: doubled-brand strip ─────────────────────────────────
def test_strips_leading_duplicate_brand():
    assert sc.canonicalize_model("Skydio", "Skydio R1") == "R1"
    assert sc.canonicalize_model("Autel", "Autel Evo II Pro 102000410") == "Evo II Pro"


def test_strips_brand_with_corporate_suffix():
    assert sc.canonicalize_model("Autel", "Autel Robotics EVO Max 4T & Speaker") == "EVO Max 4T"


def test_case_insensitive_brand_match():
    # vendor 'HoverAir' vs title 'HOVERAir'
    assert sc.canonicalize_model("HoverAir", "HOVERAir X1 Pro - Never Opened") == "X1 Pro"


def test_no_leading_brand_is_left_alone_apart_from_cruft():
    assert sc.canonicalize_model("Sony", "A7 IV") == "A7 IV"
    assert sc.canonicalize_model("Tamron", "14 150mm f3.5") == "14 150mm f3.5"


# ── canonicalize_model: trailing cruft ──────────────────────────────────────
def test_cuts_at_spec_delimiters():
    assert sc.canonicalize_model("Autel", "Autel EVO Lite 640T Enterprise | 640*512 Thermal") \
        == "EVO Lite 640T Enterprise"
    assert sc.canonicalize_model("Parrot", "Parrot ANAFI Pro Tactical & FLIR Lepton 3.5") \
        == "ANAFI Pro Tactical"


def test_cuts_at_comma_feature_list():
    assert sc.canonicalize_model("HoverAir", "HOVERAir AQUA , Waterproof , Water Takeoff") == "AQUA"


def test_strips_condition_phrase_and_sku_token():
    assert sc.canonicalize_model("Autel", "Autel Evo Nano+ Premium PRISTINE Condition") == "Evo Nano+"
    assert sc.canonicalize_model("HoverAir", "HOVERAir X1 PRO Standard, Action Flying") == "X1 PRO"


def test_strips_leading_year():
    out = sc.canonicalize_model("Autel", "2026 Autel EVO 2 Dual 640T Enterprise")
    assert out.startswith("EVO 2 Dual 640T")


# ── totality / safety ───────────────────────────────────────────────────────
def test_empty_and_whitespace_safe():
    assert sc.canonicalize_model("Sony", "") == ""
    assert sc.canonicalize_model("Sony", "   ") == ""
    assert sc.canonicalize_model("", "A7 IV") == "A7 IV"


def test_never_empties_out_identity():
    # model == vendor exactly would strip to empty → keep original as a flag
    assert sc.canonicalize_model("Skydio", "Skydio") == "Skydio"


def test_preserves_version_variant_tokens():
    # V3, 4T, 640T, Mini 3 are identity, not cruft
    out = sc.canonicalize_model("DJI", "DJI Mini 3 Pro")
    assert out == "Mini 3 Pro"


# ── is_clean_model gate ─────────────────────────────────────────────────────
def test_clean_accepts_real_models():
    assert sc.is_clean_model("Autel", "Evo II Pro")
    assert sc.is_clean_model("Skydio", "R1")
    assert sc.is_clean_model("Tamron", "14 150mm f3.5")


def test_clean_rejects_prose_survivors():
    assert not sc.is_clean_model("HoverAir", "X1 Black Foldable Mini Self Flying HDR Video")
    assert not sc.is_clean_model("HoverAir", "X1 PROMAX Flying AI Follow Foldable")


def test_clean_rejects_junk_vendor():
    assert not sc.is_clean_model("5", "Inch Droneer O4 Pro")
    assert not sc.is_clean_model("6K", "Professional Dual WIFI")


def test_clean_rejects_residual_brand():
    assert not sc.is_clean_model("HoverAir", "ZeroZero Roboics HoverAir X1")


def test_clean_rejects_overlong():
    assert not sc.is_clean_model("Autel", "EVO 2 Dual 640T Enterprise V3 Something Extra Here")
