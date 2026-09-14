"""Withheld holdout: attribution integrity — a criticism quote must be
attributed to its real source, never implicitly to AskMaddi, and the sourced
COUNT must show (counts are facts, the witness-stance discipline)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_divergence_pages as mod


def test_criticism_shows_source_and_count(tmp_path):
    out = tmp_path / "browser"
    card = {"card_id": "sony-a7iv",
            "identity": {"display_name": "Sony A7 IV", "category": "body", "brand": "Sony"},
            "synthesis": {"issue_clusters": [
                {"label": "Rolling shutter", "count": 9,
                 "quote": "very noticeable when panning", "source_id": "dpreview"}]},
            "lead_axes": [{"axis_id": "autofocus_body", "display_name": "Autofocus",
                           "convergence": 0.3, "sentiment": {"net": 0.0}}],
            "detail_axes": [], "axis_roles": {"lowest_rated": "autofocus_body"}}
    mod.build_pages([card], str(out), "https://askmaddi.com")
    html = (out / "where-they-split" / "sony-a7iv" / "index.html").read_text()
    assert "dpreview" in html.lower(), "quote not attributed to its source"
    assert "9" in html, "sourced count not shown"
