"""Tests for build_price_band_pages — the price-banded list surface."""
import json
from pathlib import Path

import build_price_band_pages as pb


def _card(cid, cat, new=None, used=None, name=None):
    pr = {}
    if new is not None:
        pr["current_new_usd"] = new
    if used is not None:
        pr["used_market"] = {"bands": {"pre_owned": used}}
    return {"card_id": cid, "identity": {"category": cat, "display_name": name or cid},
            "pricing": pr}


def test_card_price_picks_lowest_channel():
    c = _card("x", "body", new=2000, used=1200)
    assert pb.card_price(c) == (1200.0, "used")
    assert pb.card_price(_card("y", "body", new=800))[1] == "new"
    assert pb.card_price(_card("z", "body")) == (None, None)  # unpriced → omitted


def test_category_band_membership_and_thinness(tmp_path):
    cards = [_card(f"cam{i}", "body", used=p) for i, p in
             enumerate((300, 700, 900, 1800, 4000))]
    urls = pb.build_pages(cards, [], str(tmp_path), "https://x.com")
    slugs = {u.rstrip("/").rsplit("/", 1)[-1] for u in urls}
    # under-500 has only 1 card (< MIN_CARDS=3) → NOT emitted
    assert "cameras-under-500" not in slugs
    # under-1000 has 3 (300,700,900) → emitted
    assert "cameras-under-1000" in slugs
    html = (tmp_path / "under" / "cameras-under-1000" / "index.html").read_text()
    assert "cam0" in html and "cam3" not in html  # 1800 excluded from the 1000 band


def test_guide_band_preserves_guide_order_and_filters(tmp_path):
    # 4 cards; 3 fall in the $2000 band (a,b,d), c ($2500) is out. Need >=3 to emit.
    cards = [_card("a", "body", used=500), _card("b", "body", used=1500),
             _card("c", "body", used=2500), _card("d", "body", used=900)]
    guide = {"id": "cameras-for-x", "display_name": "X", "applies_to": ["body"],
             "ranked": [{"card_id": "c"}, {"card_id": "a"}, {"card_id": "b"},
                        {"card_id": "d"}]}
    pb.build_pages(cards, [guide], str(tmp_path), "https://x.com")
    p = tmp_path / "under" / "cameras-for-x-under-2000" / "index.html"
    assert p.exists()
    body = p.read_text()
    # c ($2500) filtered out; a, b, d kept in GUIDE order (a before b before d)
    assert body.index("/cards/a/") < body.index("/cards/b/") < body.index("/cards/d/")
    assert "/cards/c/" not in body


def test_present_dont_rate_no_best(tmp_path):
    import re
    cards = [_card(f"c{i}", "lens", used=p) for i, p in enumerate((100, 200, 300))]
    pb.build_pages(cards, [], str(tmp_path), "https://x.com")
    raw = (tmp_path / "under" / "lenses-under-500" / "index.html").read_text()
    # Check RENDERED PROSE only: strip <style>/<script> and all tags, so a CSS
    # property like `margin-top` can't false-positive the 'top' guardrail.
    prose = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    prose = re.sub(r"<[^>]+>", " ", prose).lower()
    for banned in ("best", "top", "#1", "winner", "worth it"):
        assert banned not in prose, f"banned phrase {banned!r} in rendered prose"


def test_hub_lists_pages(tmp_path):
    cards = [_card(f"c{i}", "body", used=p) for i, p in enumerate((100, 200, 300))]
    pb.build_pages(cards, [], str(tmp_path), "https://x.com")
    hub = (tmp_path / "under" / "index.html").read_text()
    assert "/under/cameras-under-500/" in hub
