"""C5 guide render — witness-stance rules held in HTML (spec: maddi-use-case-guides).

The render must RANK, not CROWN: no "best", no rating number, /gear-for/ URL,
and every ranked position must show its axis evidence. These are contract, not
cosmetics — the same reversal C6 made on the card.
"""
import json
import re

import build_site as bs


def _guide():
    return {
        "id": "real-estate-architecture",
        "display_name": "Real Estate & Architecture",
        "applies_to": ["body"],
        "query_aliases": ["best camera for real estate photography"],
        "criteria": [
            {"axis": "sensor_performance", "weight": 3, "display": "Sensor Performance"},
            {"axis": "image_quality", "weight": 2, "display": "Image Quality"},
        ],
        "profile_notes": "Ratified by Lee 2026-08-17",
        "ranked": [
            {"card_id": "sony-a7iv", "display_name": "Sony A7 IV", "rank": 1, "score": 3.9,
             "rationale": [
                 {"axis": "sensor_performance", "axis_display": "Sensor Performance",
                  "weight": 3, "s": 0.51, "pos": 36, "neg": 6, "total": 59,
                  "confidence": "high", "contested": False, "thin": False,
                  "contribution": 1.53, "top_sources": [
                      {"source_id": "youtube-some-reviewer-a7iv-review", "url": "u",
                       "quote": "the sensor is excellent", "witness": "firsthand"}]},
             ], "missing_axes": [], "thin_axes": [], "flags": []},
        ],
        "excluded": [{"card_id": "sony-a7s-iii", "display_name": "Sony A7S III",
                      "reasons": ["require resolution_mp >= 20: got 12"]}],
        "pending_backfill": [{"card_id": "canon-r5", "display_name": "Canon R5",
                              "unknowns": ["resolution_mp: not present on card"]}],
        "counts": {"in_scope": 3, "ranked": 1, "excluded": 1, "pending_backfill": 1},
    }


def _cards_by_id():
    return {"sony-a7iv": {"card_id": "sony-a7iv",
                          "identity": {"display_name": "Sony A7 IV", "category": "body"},
                          "pricing": {}}}


def test_no_best_in_prose():
    html = bs.render_guide(_guide(), _cards_by_id())
    assert not re.search(r"\bbest\b", html, re.I)


def test_no_rating_value_anywhere():
    html = bs.render_guide(_guide(), _cards_by_id())
    assert "ratingValue" not in html and "AggregateRating" not in html


def test_canonical_is_gear_for_not_best():
    html = bs.render_guide(_guide(), _cards_by_id())
    assert 'href="https://askmaddi.com/gear-for/real-estate-architecture/"' in html
    assert "/best/" not in html


def test_jsonld_is_itemlist_no_ratings():
    html = bs.render_guide(_guide(), _cards_by_id())
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    obj = json.loads(m.group(1))
    assert obj["@type"] == "ItemList"
    assert obj["numberOfItems"] == 1
    assert "ating" not in json.dumps(obj)  # no rating/ratingValue leaked


def test_ranked_position_shows_axis_evidence():
    html = bs.render_guide(_guide(), _cards_by_id())
    # the rank shows the axis, its net %, and the pos/neg/total behind it
    assert "Sensor Performance" in html
    assert "+51%" in html
    assert "36▲" in html and "of 59" in html


def test_excluded_and_pending_rendered_apart():
    html = bs.render_guide(_guide(), _cards_by_id())
    assert "Not ranked" in html and "Sony A7S III" in html
    assert "still sourcing" in html and "Canon R5" in html


def test_ranked_card_links_to_its_card_page():
    html = bs.render_guide(_guide(), _cards_by_id())
    assert 'href="/cards/sony-a7iv/"' in html


def test_guide_in_sitemap():
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as d:
        p = bs.write_sitemap(d, [], guides=[_guide()])
        xml = pathlib.Path(p).read_text()
        assert "https://askmaddi.com/gear-for/real-estate-architecture/" in xml


def test_guide_in_llms_txt_with_ranked_products():
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as d:
        p = bs.write_llms_txt(d, [], guides=[_guide()])
        txt = pathlib.Path(p).read_text()
        assert "## Buying guides" in txt
        # the map an LLM reads: use-case URL, criteria, and the ranked products
        assert "https://askmaddi.com/gear-for/real-estate-architecture/" in txt
        assert "ranked on Sensor Performance" in txt
        assert "1. Sony A7 IV" in txt
        # witness-stance holds in the ingestion surface too
        assert "best" not in txt.lower()


def test_llms_txt_omits_guides_section_when_none():
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as d:
        p = bs.write_llms_txt(d, [], guides=None)
        assert "## Buying guides" not in pathlib.Path(p).read_text()
