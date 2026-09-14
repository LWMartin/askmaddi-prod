"""Withheld holdout: the guide link text must be the guide's display_name (human
+ crawler-legible anchor), and every emitted link points at /gear-for/<id>/."""
import sys, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_usecase_fit_pages as mod


def test_anchor_text_is_guide_display_name(tmp_path):
    out = tmp_path / "browser"
    cards = [{"card_id": "sony-a7iv",
              "identity": {"display_name": "Sony A7 IV", "category": "body", "brand": "Sony"}}]
    guides = [{"id": "cameras-for-astrophotography", "display_name": "Astrophotography",
               "applies_to": ["body"], "ranked": [{"card_id": "sony-a7iv"}]}]
    mod.build_pages(cards, guides, str(out), "https://askmaddi.com")
    html = (out / "fit" / "sony-a7iv" / "index.html").read_text()
    assert "Astrophotography" in html
    assert "/gear-for/cameras-for-astrophotography/" in html
