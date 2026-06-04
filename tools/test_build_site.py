"""Tests for the teaser axis role-selection design (2026-06-03).

Design rules under test:
  1. Three roles: most_discussed (volume), highest_rated (best pos-ratio),
     biggest_gripe (worst pos-ratio).
  2. Volume floor: total >= max(15, 0.1 * top axis volume) — keeps noise
     axes out of the high/low slots.
  3. Meta-axes (generation_context, price) excluded from high/low slots,
     eligible for most_discussed.
  4. Collisions take the next distinct axis; sparse cards fall back to
     volume order with role=None.

Run from repo root:  python -m pytest tools/test_build_site.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_site import (  # noqa: E402
    select_teaser_axes, teaser_entry,
    TEASER_ROLE_MOST, TEASER_ROLE_HIGH, TEASER_ROLE_LOW,
)


def _axis(axis_id, pos, neg, neu=0, display=None):
    total = pos + neg + neu
    return {
        "axis_id": axis_id,
        "display_name": display or axis_id.replace("_", " ").title(),
        "sentiment": {"pos": pos, "neg": neg, "neu": neu, "total": total},
    }


def _card(lead, detail=None, card_id="test-sku"):
    return {
        "card_id": card_id,
        "identity": {"display_name": "Test", "brand": "T",
                     "category": "x", "subcategory": "y"},
        "lead_axes": lead,
        "detail_axes": detail or [],
        "sources": [],
    }


def _roles(picks):
    return {role: a["axis_id"] for a, role in picks if role}


def test_three_roles_assigned():
    card = _card([
        _axis("video", 85, 150),     # volume 235 -> most discussed
        _axis("autofocus", 64, 38),  # 63% -> highest rated
        _axis("buffer", 42, 140),    # 23% -> biggest gripe
        _axis("handling", 52, 49),
    ])
    r = _roles(select_teaser_axes(card))
    assert r[TEASER_ROLE_MOST] == "video"
    assert r[TEASER_ROLE_HIGH] == "autofocus"
    assert r[TEASER_ROLE_LOW] == "buffer"


def test_volume_floor_excludes_noise_axes():
    """A tiny all-negative axis must not headline biggest_gripe, and a tiny
    all-positive axis must not headline highest_rated."""
    card = _card([
        _axis("video", 100, 142),    # 242 total -> floor = 24.2
        _axis("autofocus", 64, 38),
        _axis("buffer", 42, 140),
        _axis("weight", 0, 4),       # 4 < floor: 0% but ineligible
        _axis("build", 20, 2),       # 22 < floor: 91% but ineligible
    ])
    r = _roles(select_teaser_axes(card))
    assert r[TEASER_ROLE_LOW] == "buffer"      # not weight
    assert r[TEASER_ROLE_HIGH] == "autofocus"  # not build


def test_meta_axes_excluded_from_high_low_but_allowed_as_most_discussed():
    card = _card([
        _axis("generation_context", 90, 10),  # 90%: would win HIGH if eligible
        _axis("price", 2, 78),                # 2.5%: would win LOW if eligible
        _axis("autofocus", 60, 40),
        _axis("buffer", 30, 70),
    ])
    r = _roles(select_teaser_axes(card))
    assert r[TEASER_ROLE_MOST] == "generation_context"  # volume winner, eligible here
    assert r[TEASER_ROLE_HIGH] == "autofocus"
    assert r[TEASER_ROLE_LOW] == "buffer"


def test_collision_takes_next_distinct_axis():
    """When the most-discussed axis is also the worst-rated, the gripe slot
    takes the next-worst distinct axis rather than repeating."""
    card = _card([
        _axis("video", 20, 180),     # most discussed AND worst (10%)
        _axis("autofocus", 64, 38),
        _axis("buffer", 42, 100),    # 30% -> next-worst distinct
    ])
    picks = select_teaser_axes(card)
    ids = [a["axis_id"] for a, _ in picks]
    assert len(ids) == len(set(ids)), "an axis filled two slots"
    r = _roles(picks)
    assert r[TEASER_ROLE_MOST] == "video"
    assert r[TEASER_ROLE_LOW] == "buffer"


def test_sparse_card_falls_back_to_volume_order_unlabeled():
    """One qualifying axis + tiny axes: remaining slots fill by volume with
    role=None so the renderer shows no label on them."""
    card = _card([
        _axis("video", 100, 100),  # 200 -> floor 20; only qualifier
        _axis("weight", 1, 5),     # 6
        _axis("build", 3, 1),      # 4
    ])
    picks = select_teaser_axes(card)
    assert len(picks) == 3
    assert picks[0][1] == TEASER_ROLE_MOST
    assert picks[1] == (card["lead_axes"][1], None)  # weight, unlabeled
    assert picks[2] == (card["lead_axes"][2], None)  # build, unlabeled


def test_detail_axes_scanned_too():
    """High/low candidates come from the full card, not just lead_axes."""
    card = _card(
        lead=[_axis("video", 85, 150), _axis("handling", 52, 49)],
        detail=[_axis("evf", 45, 10)],  # 82% in detail_axes -> highest rated
    )
    r = _roles(select_teaser_axes(card))
    assert r[TEASER_ROLE_HIGH] == "evf"


def test_teaser_entry_carries_role_field():
    card = _card([
        _axis("video", 85, 150),
        _axis("autofocus", 64, 38),
        _axis("buffer", 42, 140),
    ])
    entry = teaser_entry(card)
    roles = [a["role"] for a in entry["top_axes"]]
    assert roles == [TEASER_ROLE_MOST, TEASER_ROLE_HIGH, TEASER_ROLE_LOW]
    # Shape stays backward compatible: axis/pos/neg/total all present.
    for a in entry["top_axes"]:
        for k in ("axis", "pos", "neg", "total"):
            assert k in a


def test_empty_card_yields_no_axes():
    assert select_teaser_axes(_card([])) == []


# ─── Affiliate tag enforcement (revenue regression: untagged /dp/ CTAs) ─────
from build_site import ensure_affiliate_tag, new_cta, used_cta


def test_raw_amazon_product_url_gets_tagged():
    out = ensure_affiliate_tag("https://www.amazon.com/dp/B09JZT6XXX")
    assert "tag=askmaddi-20" in out


def test_already_tagged_amazon_url_not_doubled():
    url = "https://www.amazon.com/dp/B09JZT6XXX?tag=askmaddi-20"
    out = ensure_affiliate_tag(url)
    assert out.count("tag=askmaddi-20") == 1


def test_raw_ebay_item_url_gets_campaign_params():
    out = ensure_affiliate_tag("https://www.ebay.com/itm/123456789")
    assert "campid=5339138080" in out and "mkrid=711-53200-19255-0" in out


def test_non_program_domain_passthrough():
    url = "https://www.bhphotovideo.com/c/product/123"
    assert ensure_affiliate_tag(url) == url


def test_new_cta_tags_current_new_url_when_affiliate_url_null():
    card = {
        "identity": {"display_name": "Sony A7 IV"},
        "pricing": {
            "affiliate_url": None,
            "current_new_url": "https://www.amazon.com/dp/B09JZT6XXX",
            "current_new_usd": 2498,
        },
    }
    _, url = new_cta(card)
    assert "tag=askmaddi-20" in url


def test_used_cta_tags_raw_search_url():
    card = {
        "identity": {"display_name": "Sony A7 IV"},
        "pricing": {
            "used_market": {
                "affiliate_url": None,
                "search_url": "https://www.ebay.com/sch/i.html?_nkw=sony+a7+iv",
            }
        },
    }
    _, url = used_cta(card)
    assert "campid=5339138080" in url
