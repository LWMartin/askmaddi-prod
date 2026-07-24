"""Tests for the facts-pipeline -> Specifications render seam (2026-07-24).

Design under test (fact-pipeline §3):
  1. Specs are read from `facts.specs` ({slug: FactValue}), not top-level.
  2. FactValue formatting: numeric anchor shown first ("665 g"); a genuine
     [low, high] spread is appended only when sources disagree; categorical
     facts use `value`; unit annotates numeric facts.
  3. Slug labels are humanized, with canonical casing for known acronyms.
  4. Absent/empty facts -> the section is omitted entirely (no empty box).
  5. Provenance (§3.2/§8) is surfaced as a subtle 'specs: <sources>' line.
  6. Legacy flat top-level `specs` dicts still render (backward compat).

Run from repo root:  python -m pytest tools/test_build_site_specs.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_site import specs_section, _fact_display, _spec_label  # noqa: E402


def _fv(value=None, low=None, high=None, anchor=None, anchor_source="", unit=""):
    return {"value": value, "low": low, "high": high, "anchor": anchor,
            "anchor_source": anchor_source, "unit": unit}


def _card(specs=None, provenance=None):
    return {"facts": {"specs": specs or {},
                      "provenance": provenance or {},
                      "conflicts": []}}


# ── FactValue formatting ────────────────────────────────────────────────

def test_numeric_single_value_shows_anchor_and_unit():
    assert _fact_display(_fv(anchor=665, low=665, high=665, unit="g")) == "665 g"


def test_numeric_spread_appends_range_only_when_sources_disagree():
    out = _fact_display(_fv(anchor=665, low=660, high=670, unit="g"))
    assert out == "665 g (660–670)"


def test_numeric_float_drops_trailing_zero_keeps_real_decimal():
    assert _fact_display(_fv(anchor=665.0, low=665.0, high=665.0, unit="g")) == "665 g"
    assert _fact_display(_fv(anchor=66.5, low=66.5, high=66.5, unit="mm")) == "66.5 mm"


def test_categorical_value_renders():
    assert _fact_display(_fv(value="Sony E-mount")) == "Sony E-mount"


def test_empty_factvalue_renders_nothing():
    assert _fact_display(_fv()) == ""
    assert _fact_display(None) == ""


def test_legacy_scalar_value_still_renders():
    assert _fact_display("28.2 MP") == "28.2 MP"


# ── slug labels ─────────────────────────────────────────────────────────

def test_slug_humanized():
    assert _spec_label("sensor_resolution") == "Sensor Resolution"


def test_slug_acronym_casing_preserved():
    assert _spec_label("iso") == "ISO"
    assert _spec_label("max_iso") == "Max ISO"


# ── section assembly ────────────────────────────────────────────────────

def test_section_reads_facts_specs_location():
    card = _card(specs={"weight": _fv(anchor=665, low=665, high=665, unit="g"),
                        "mount": _fv(value="Sony E-mount")})
    html = specs_section(card)
    assert "Specifications" in html
    assert "665 g" in html
    assert "Sony E-mount" in html
    assert "Weight" in html and "Mount" in html


def test_absent_facts_omits_section():
    assert specs_section({"facts": {"specs": {}, "provenance": {}}}) == ""
    assert specs_section({}) == ""


def test_all_empty_factvalues_omit_section():
    assert specs_section(_card(specs={"weight": _fv(), "mount": _fv()})) == ""


def test_provenance_line_lists_distinct_sources_in_order():
    card = _card(
        specs={"weight": _fv(anchor=665, low=665, high=665, unit="g")},
        provenance={"specs.weight": {"source": "manufacturer"},
                    "specs.mount": {"source": "wikidata"},
                    "specs.iso": {"source": "manufacturer"}},
    )
    html = specs_section(card)
    assert "specs: manufacturer · wikidata" in html


def test_no_provenance_no_subtitle():
    card = _card(specs={"weight": _fv(anchor=665, low=665, high=665, unit="g")})
    assert "spec-provenance" not in specs_section(card)


def test_legacy_top_level_specs_still_render():
    card = {"specs": {"Weight": "665 g", "Mount": "Sony E-mount"}}
    html = specs_section(card)
    assert "665 g" in html and "Sony E-mount" in html


def test_value_is_html_escaped():
    card = _card(specs={"mount": _fv(value="<script>x</script>")})
    html = specs_section(card)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# ── § wire — identity.* provenance must NOT leak into the "specs:" line ──
from build_site import _specs_provenance_line  # noqa: E402


def test_specs_provenance_line_ignores_identity_entries():
    # facts.provenance now carries identity.* fold entries (image/gtin/mpn).
    # The 'specs:' subtitle must reflect only the sources of actual SPECS.
    facts = {"provenance": {
        "specs.weight": {"source": "curated"},
        "identity.gtin": {"source": "wikidata"},
        "identity.image_thumb": {"source": "wikidata"},
    }}
    line = _specs_provenance_line(facts)
    assert "curated" in line
    assert "wikidata" not in line  # identity source did not leak in


def test_specs_provenance_line_empty_when_only_identity():
    facts = {"provenance": {"identity.gtin": {"source": "wikidata"}}}
    assert _specs_provenance_line(facts) == ""
