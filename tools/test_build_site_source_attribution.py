"""C9 (use-case-guides Phase-0): source-name attribution in featured quotes.

Regression for the 'Rss Dpreview Gps Media The Photos That Made ...' byline
garble that rendered on 10/12 live cards: a face_quote without an inline
reviewer fell straight to _reviewer_from_source_id, which title-cased the whole
RSS source_id slug (publisher + article headline). The curated reviewer name
lives in the card's sources[]; axis_block now resolves it there first.

Run from repo root:  python -m pytest tools/test_build_site_source_attribution.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_site import (  # noqa: E402
    _reviewer_from_source_id, _source_reviewer_map, axis_block,
)


# ─── _source_reviewer_map ────────────────────────────────────────────────────

def test_map_keeps_only_sources_with_reviewer():
    sources = [
        {"source_id": "a", "reviewer": "DPReview (GPS Media)"},
        {"source_id": "b", "reviewer": ""},      # empty -> skipped
        {"source_id": "c"},                       # missing -> skipped
        {"reviewer": "No Id"},                    # no id -> skipped
    ]
    assert _source_reviewer_map(sources) == {"a": "DPReview (GPS Media)"}


def test_map_tolerates_none():
    assert _source_reviewer_map(None) == {}


# ─── _reviewer_from_source_id fallback hardening ─────────────────────────────

def test_rss_prefix_stripped_and_not_word_soup():
    garble_id = ("rss-dpreview-gps-media-the-photos-that-made-"
                 "photographers-fall-in-love-with-their-n-2026")
    name = _reviewer_from_source_id(garble_id)
    # The old bug produced the full headline title-cased. Now: 'rss' is gone
    # and the byline is capped to a name's worth of tokens.
    assert not name.lower().startswith("rss")
    assert len(name.split()) <= 4
    assert "Photographers" not in name  # headline no longer spills in


def test_youtube_person_still_parses():
    assert _reviewer_from_source_id(
        "youtube-christopher-frost-sigma-35mm-f12-dg-ii") == "Christopher Frost"


def test_empty_source_id_is_source():
    assert _reviewer_from_source_id("") == "source"


# ─── axis_block reviewer resolution order ────────────────────────────────────

def _axis(source_id, reviewer=None):
    fq = {"quote_excerpt": "Sharp wide open.", "url": "https://x/y",
          "source_id": source_id}
    if reviewer is not None:
        fq["reviewer"] = reviewer
    return {"name": "sharpness", "pos": 8, "neu": 1, "neg": 1, "face_quote": fq}


def test_axis_prefers_inline_reviewer():
    html = axis_block(_axis("rss-whatever", reviewer="Inline Name"),
                      {"rss-whatever": "Map Name"})
    assert "Inline Name" in html
    assert "Map Name" not in html


def test_axis_resolves_curated_name_from_source_map():
    sid = "rss-dpreview-gps-media-the-photos-that-made-photographers-n-2026"
    html = axis_block(_axis(sid), {sid: "DPReview (GPS Media)"})
    assert "DPReview (GPS Media)" in html
    # The exact garble must not survive.
    assert "Rss Dpreview Gps Media" not in html


def test_axis_falls_back_to_slug_when_no_map_hit():
    html = axis_block(_axis("youtube-jared-polin-sigma-35mm"), {})
    assert "Jared Polin" in html


def test_axis_no_source_map_arg_still_works():
    # Back-compat: axis_block(axis) with no map must not crash.
    html = axis_block(_axis("youtube-gordon-laing-sigma-35mm"))
    assert "Gordon Laing" in html
