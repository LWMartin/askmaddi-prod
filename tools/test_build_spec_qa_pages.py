"""Contract for build_spec_qa_pages — factual per-card spec Q&A with FAQPage
JSON-LD. Only present specs get answered (honest-door); neutral voice."""
import sys, json, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_spec_qa_pages as mod

BANNED = ("best", "top", "#1", "winner", "worth it")


def _card(cid, specs):
    return {
        "card_id": cid,
        "identity": {"display_name": cid.replace("-", " ").title(),
                     "category": "body", "brand": "Acme"},
        "facts": {"specs": {k: {"value": v} for k, v in specs.items()}},
    }


def test_emits_qa_from_present_specs_only(tmp_path):
    out = tmp_path / "browser"
    c = _card("sony-a7iv", {"resolution_mp": "33 megapixels",
                            "card_slots": "Dual SD card slots",
                            "weight_g": "658 g"})
    paths = mod.build_pages([c], str(out), "https://askmaddi.com")
    assert len(paths) == 1
    html = (out / "specs" / "sony-a7iv" / "index.html").read_text()
    # a present spec is answered; the question names the product
    assert "33" in html and ("card slot" in html.lower())
    assert "Sony A7Iv" in html or "sony-a7iv" in html
    # links back to the card
    assert "/cards/sony-a7iv/" in html
    # FAQPage JSON-LD present and parseable
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    assert m, "no JSON-LD block"
    data = json.loads(m.group(1))
    blob = json.dumps(data)
    assert "FAQPage" in blob and "Question" in blob


def test_absent_spec_never_fabricated(tmp_path):
    out = tmp_path / "browser"
    c = _card("nikon-zf", {"weight_g": "710 g"})  # only weight present
    mod.build_pages([c], str(out), "https://askmaddi.com")
    html = (out / "specs" / "nikon-zf" / "index.html").read_text().lower()
    # nothing about card slots / resolution should be invented
    assert "card slot" not in html
    assert "megapixel" not in html and "resolution" not in html


def test_card_with_no_mappable_specs_skipped(tmp_path):
    out = tmp_path / "browser"
    c = _card("weird-cam", {"unmapped_key_xyz": "value"})
    paths = mod.build_pages([c], str(out), "https://askmaddi.com")
    assert paths == []


def test_voice_neutral(tmp_path):
    out = tmp_path / "browser"
    mod.build_pages([_card("sony-a7iv", {"resolution_mp": "33 megapixels"})],
                    str(out), "https://askmaddi.com")
    html = (out / "specs" / "sony-a7iv" / "index.html").read_text().lower()
    for w in BANNED:
        assert w not in html
