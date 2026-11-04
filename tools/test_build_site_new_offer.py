"""Tests for the priced-New schema.org Offer (2026-11-04).

Pins: an Adorama Offer appears ONLY for a feed-sourced price (never MSRP, a
hand-set price, a withdrawn price, or Amazon); its price/url are the visible
button's; a used-only card keeps its single AggregateOffer object unchanged;
new + used becomes a two-item list. One test runs across the real banked
catalog so a schema/visible-price mismatch can never ship unnoticed."""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_site import new_cta, new_offer, schema_org_jsonld  # noqa: E402
from test_build_site_c6_review_authority import _card  # noqa: E402

AFF = ("https://adorama.prf.hn/click/camref:x/destination:"
       "https%3A%2F%2Fwww.adorama.com%2Fisoa7m4")
CANON = "https://askmaddi.com/cards/x/"


def _priced(price=1998.0, source="adorama_feed", used_bands=None, **extra):
    card = _card()
    card["pricing"] = {"current_new_usd": price, "current_new_source": source,
                       "affiliate_url": AFF, "current_new_url": AFF,
                       "used_market": {"bands": used_bands or {}}, **extra}
    return card


def _ld(card):
    return json.loads(schema_org_jsonld(card, CANON, None, "d"))


def test_feed_price_emits_offer_matching_the_visible_button():
    card = _priced()
    label, url = new_cta(card)
    offer = _ld(card)["offers"]
    assert offer["@type"] == "Offer"
    assert offer["price"] == "1998.00" and label == "$1998 new"
    assert offer["url"] == url == AFF
    assert offer["priceCurrency"] == "USD"
    assert offer["availability"] == "https://schema.org/InStock"
    assert offer["itemCondition"] == "https://schema.org/NewCondition"
    assert offer["seller"] == {"@type": "Organization", "name": "Adorama"}


def test_fractional_feed_price_uses_the_rendered_whole_dollar():
    card = _priced(price=1997.95)
    assert new_cta(card)[0] == "$1997 new"
    assert new_offer(card)["price"] == "1997.00"


def test_no_offer_for_withdrawn_hand_set_or_msrp_only_prices():
    assert new_offer(_priced(price=None)) is None                 # withdrawn
    assert new_offer(_priced(price=0)) is None
    assert new_offer(_priced(source="manual")) is None             # hand-set
    msrp_only = _priced(price=None, msrp_usd=2499)
    assert new_cta(msrp_only)[0] == "$2499 new"                    # label may show MSRP...
    assert new_offer(msrp_only) is None                            # ...but it is never an Offer
    assert "offers" not in _ld(_priced(price=None))


def test_used_only_card_keeps_single_aggregate_offer_shape():
    obj = _ld(_priced(price=None, used_bands={"good": 1400.0, "excellent": 1600.0}))
    assert isinstance(obj["offers"], dict) and obj["offers"]["@type"] == "AggregateOffer"


def test_new_and_used_become_a_list_new_first():
    obj = _ld(_priced(used_bands={"good": 1400.0}))
    assert [o["@type"] for o in obj["offers"]] == ["Offer", "AggregateOffer"]


def test_amazon_never_appears_in_structured_data():
    card = _priced(used_bands={"good": 1400.0})
    card["pricing"]["amazon_asin"] = "B09JZT6YK5"
    assert "amazon" not in schema_org_jsonld(card, CANON, None, "d").lower()


_CARDS = Path(__file__).resolve().parent.parent / "data" / "cards"


def test_real_catalog_offer_parity_with_visible_button():
    """Every Offer the real catalog emits matches its rendered button exactly."""
    checked = 0
    for path in sorted(_CARDS.glob("*.json")):
        card = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(card.get("identity"), dict):
            continue
        offer = new_offer(card)
        if not offer:
            continue
        label, url = new_cta(card)
        m = re.fullmatch(r"\$(\d+) new", label)
        assert m, f"{path.stem}: offer emitted but button is {label!r}"
        assert offer["price"] == f"{m.group(1)}.00", path.stem
        assert offer["url"] == url, path.stem
        checked += 1
    assert checked > 0, "no priced cards found — fixture path wrong?"
