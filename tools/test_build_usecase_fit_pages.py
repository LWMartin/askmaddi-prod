"""Contract for build_usecase_fit_pages — the card->guide linking backbone.
Per card, links to every guide that ranks it. Voice: 'suited to / appears in',
never 'best'."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_usecase_fit_pages as mod

BANNED = ("best", "top", "#1", "winner", "worth it")


def _card(cid):
    return {"card_id": cid,
            "identity": {"display_name": cid.replace("-", " ").title(),
                         "category": "body", "brand": "Acme"}}


def _guide(gid, disp, ranked_ids):
    return {"id": gid, "display_name": disp, "applies_to": ["body"],
            "ranked": [{"card_id": c} for c in ranked_ids]}


def test_links_card_to_every_guide_that_ranks_it(tmp_path):
    out = tmp_path / "browser"
    cards = [_card("sony-a7iv"), _card("canon-r6")]
    guides = [_guide("cameras-for-astrophotography", "Astrophotography",
                     ["sony-a7iv", "canon-r6"]),
              _guide("cameras-for-street", "Street Photography", ["sony-a7iv"])]
    paths = mod.build_pages(cards, guides, str(out), "https://askmaddi.com")
    page = out / "fit" / "sony-a7iv" / "index.html"
    assert page.exists()
    html = page.read_text()
    # a7iv is in BOTH guides -> both linked
    assert "/gear-for/cameras-for-astrophotography/" in html
    assert "/gear-for/cameras-for-street/" in html
    # r6 only in astro -> street link absent on its page
    r6 = (out / "fit" / "canon-r6" / "index.html").read_text()
    assert "/gear-for/cameras-for-astrophotography/" in r6
    assert "/gear-for/cameras-for-street/" not in r6
    # links back to the card + a hub exists
    assert "/cards/sony-a7iv/" in html
    assert (out / "fit" / "index.html").exists()


def test_card_in_no_guide_is_skipped(tmp_path):
    out = tmp_path / "browser"
    cards = [_card("orphan-cam")]
    guides = [_guide("cameras-for-astrophotography", "Astrophotography", ["sony-a7iv"])]
    paths = mod.build_pages(cards, guides, str(out), "https://askmaddi.com")
    assert paths == []
    assert not (out / "fit" / "orphan-cam").exists()


def test_shows_position_not_crown(tmp_path):
    out = tmp_path / "browser"
    cards = [_card("canon-r6")]
    guides = [_guide("cameras-for-astrophotography", "Astrophotography",
                     ["sony-a7iv", "canon-r6", "nikon-z6"])]  # r6 is position 2 of 3
    mod.build_pages(cards, guides, str(out), "https://askmaddi.com")
    html = (out / "fit" / "canon-r6" / "index.html").read_text().lower()
    for w in BANNED:
        assert w not in html
    # position is presented (2 of 3) — a coordinate, not a verdict
    assert "2" in html and "3" in html
