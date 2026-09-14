"""Contract for build_divergence_pages — the 'Where reviewers push back' surface.
Worker builds tools/build_divergence_pages.py to satisfy this. Voice: report,
never rate."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_divergence_pages as mod

BANNED = ("best", "top", "#1", "winner", "worth it")


def _card(cid, cat="body", issues=None, axes=None, lowest=None):
    return {
        "card_id": cid,
        "identity": {"display_name": cid.replace("-", " ").title(),
                     "category": cat, "brand": "Acme"},
        "synthesis": {"issue_clusters": issues or []},
        "lead_axes": axes or [],
        "detail_axes": [],
        "axis_roles": {"lowest_rated": lowest},
    }


def _rich(cid="sony-a7iv"):
    return _card(
        cid,
        issues=[{"label": "Rolling shutter in video", "count": 9,
                 "quote": "the rolling shutter is very noticeable when panning",
                 "source_id": "dpreview"}],
        axes=[{"axis_id": "autofocus_body", "display_name": "Autofocus",
               "convergence": 0.35, "sentiment": {"net": 0.1}},
              {"axis_id": "image_quality", "display_name": "Image Quality",
               "convergence": 0.95, "sentiment": {"net": 0.8}}],
        lowest="autofocus_body")


def test_emits_per_card_page_and_hub(tmp_path):
    out = tmp_path / "browser"
    paths = mod.build_pages([_rich("sony-a7iv"), _rich("canon-r6")],
                            str(out), "https://askmaddi.com")
    assert len(paths) == 2
    page = out / "where-they-split" / "sony-a7iv" / "index.html"
    assert page.exists()
    hub = out / "where-they-split" / "index.html"
    assert hub.exists()
    html = page.read_text()
    # sourced criticism + the split axis + the debated aspect surface
    assert "Rolling shutter" in html
    assert "Autofocus" in html
    # links back to the card and to the hub
    assert "/cards/sony-a7iv/" in html
    assert "/where-they-split/" in html
    # hub lists both cards
    hub_html = hub.read_text()
    assert "sony-a7iv" in hub_html and "canon-r6" in hub_html


def test_voice_never_rates(tmp_path):
    out = tmp_path / "browser"
    mod.build_pages([_rich()], str(out), "https://askmaddi.com")
    html = (out / "where-they-split" / "sony-a7iv" / "index.html").read_text().lower()
    for w in BANNED:
        assert w not in html, f"banned verdict word rendered: {w!r}"


def test_thin_card_is_skipped(tmp_path):
    out = tmp_path / "browser"
    # no issues, no low-convergence axis -> nothing honest to say -> skip
    thin = _card("nikon-zf", issues=[],
                 axes=[{"axis_id": "build", "display_name": "Build",
                        "convergence": 0.98, "sentiment": {"net": 0.7}}],
                 lowest=None)
    paths = mod.build_pages([thin], str(out), "https://askmaddi.com")
    assert paths == []
    assert not (out / "where-they-split" / "nikon-zf").exists()


def test_deterministic(tmp_path):
    out1 = tmp_path / "a"; out2 = tmp_path / "b"
    cards = [_rich("sony-a7iv"), _rich("canon-r6")]
    mod.build_pages(cards, str(out1), "https://askmaddi.com")
    mod.build_pages(cards, str(out2), "https://askmaddi.com")
    a = (out1 / "where-they-split" / "sony-a7iv" / "index.html").read_text()
    b = (out2 / "where-they-split" / "sony-a7iv" / "index.html").read_text()
    assert a == b
