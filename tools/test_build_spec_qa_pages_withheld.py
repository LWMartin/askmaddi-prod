"""Withheld holdout: each FAQ question must NAME the product (a spec Q&A page is
per-product, not a generic question), so the long-tail query match is real."""
import sys, re, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_spec_qa_pages as mod


def test_question_names_the_product(tmp_path):
    out = tmp_path / "browser"
    card = {"card_id": "sony-a7iv",
            "identity": {"display_name": "Sony A7 IV", "category": "body", "brand": "Sony"},
            "facts": {"specs": {"card_slots": {"value": "Dual SD card slots"}}}}
    mod.build_pages([card], str(out), "https://askmaddi.com")
    html = (out / "specs" / "sony-a7iv" / "index.html").read_text()
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    blob = json.dumps(json.loads(m.group(1)))
    assert "Sony A7 IV" in blob, "FAQ question does not name the product"
