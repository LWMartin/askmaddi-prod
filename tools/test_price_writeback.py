"""Tests for price_writeback — apply the Adorama New-price backfill onto cards.

Pins: dry run mutates nothing; --apply writes only the four New pricing fields
(used_market untouched); needs_eyes rows are held unless --include-flagged; the
result is what build_site.new_cta reads back as a live '$N new' CTA."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import price_writeback  # noqa: E402


def _card(pricing_extra=None):
    p = {"current_new_usd": 0.0, "current_new_source": None, "current_new_url": None,
         "affiliate_url": None, "used_market": {"bands": {"pre_owned": 250.0},
                                                "price_updated_at": "2026-08-31T00:00:00Z"}}
    if pricing_extra:
        p.update(pricing_extra)
    return {"card_id": "x", "identity": {"display_name": "Canon EOS R5"}, "pricing": p}


def _sheet(*proposals):
    return {"proposals": list(proposals)}


AFF = "https://adorama.prf.hn/click/camref:x/destination:https://www.adorama.com/r5.html"


def _setup(tmp_path, slug="canon-r5", card=None):
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / f"{slug}.json").write_text(json.dumps(card or _card(), indent=2), encoding="utf-8")
    return cards


def test_dry_run_writes_nothing(tmp_path):
    cards = _setup(tmp_path)
    before = (cards / "canon-r5.json").read_text()
    sheet = _sheet({"slug": "canon-r5", "has_card": True, "needs_eyes": False,
                    "new": {"matched_by": "gtin", "price_usd": 3899.0, "affiliate_url": AFF}})
    changes, held, skipped = price_writeback.plan(sheet, cards)
    assert len(changes) == 1 and not held
    assert (cards / "canon-r5.json").read_text() == before  # plan() never writes


def test_apply_writes_only_new_fields(tmp_path):
    cards = _setup(tmp_path)
    sheet = _sheet({"slug": "canon-r5", "has_card": True, "needs_eyes": False,
                    "new": {"matched_by": "gtin", "price_usd": 3899.0, "affiliate_url": AFF}})
    changes, _, _ = price_writeback.plan(sheet, cards)
    price_writeback.apply_changes(changes, cards)
    p = json.loads((cards / "canon-r5.json").read_text())["pricing"]
    assert p["current_new_usd"] == 3899
    assert p["affiliate_url"] == AFF and p["current_new_url"] == AFF
    assert p["current_new_source"] == "adorama_feed"
    # used_market is left exactly as it was — the richer eBay source wins.
    assert p["used_market"]["bands"] == {"pre_owned": 250.0}


def test_needs_eyes_is_held(tmp_path):
    cards = _setup(tmp_path)
    sheet = _sheet({"slug": "canon-r5", "has_card": True, "needs_eyes": True,
                    "new": {"matched_by": "mpn", "price_usd": 3899.0, "affiliate_url": AFF,
                            "ambiguous": True, "gtin_agree": False}})
    changes, held, _ = price_writeback.plan(sheet, cards)
    assert not changes and len(held) == 1
    changes2, held2, _ = price_writeback.plan(sheet, cards, include_flagged=True)
    assert len(changes2) == 1 and not held2


def test_missing_card_is_skipped_not_fatal(tmp_path):
    cards = _setup(tmp_path)
    sheet = _sheet({"slug": "ghost", "has_card": True, "needs_eyes": False,
                    "new": {"matched_by": "gtin", "price_usd": 100.0, "affiliate_url": AFF}})
    changes, held, skipped = price_writeback.plan(sheet, cards)
    assert not changes and len(skipped) == 1 and skipped[0]["slug"] == "ghost"


def test_no_new_match_or_no_card_ignored(tmp_path):
    cards = _setup(tmp_path)
    sheet = _sheet(
        {"slug": "canon-r5", "has_card": True, "needs_eyes": False, "new": None},   # used-only
        {"slug": "canon-r5", "has_card": False, "needs_eyes": False,
         "new": {"matched_by": "gtin", "price_usd": 1.0, "affiliate_url": AFF}},     # no card
    )
    changes, held, skipped = price_writeback.plan(sheet, cards)
    assert not changes and not held and not skipped


def test_patch_manifest_is_surgical(tmp_path):
    # Manifest with two entries; only the changed slug's New fields must move,
    # everything else (other entry, used/amazon fields, generated_at) byte-stable.
    manifest = {"generated_at": "2026-09-02", "cards": [
        {"card_id": "canon-r5", "display_name": "Canon R5",
         "pricing": {"new_price": 0, "new_url": "search", "used_price": 250,
                     "used_url": "ebay", "amazon_url": "amz"}},
        {"card_id": "other", "display_name": "Other",
         "pricing": {"new_price": 0, "new_url": "search-other", "used_price": 99}},
    ]}
    mp = tmp_path / "cards-manifest.json"
    mp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    changes = [{"slug": "canon-r5", "new": 3899, "url": AFF}]
    res = price_writeback.patch_manifest(mp, changes)
    assert res["patched"] == ["canon-r5"] and not res["absent"]
    out = json.loads(mp.read_text())
    r5 = next(c for c in out["cards"] if c["card_id"] == "canon-r5")
    assert r5["pricing"]["new_price"] == 3899 and r5["pricing"]["new_url"] == AFF
    assert r5["pricing"]["used_price"] == 250  # untouched
    other = next(c for c in out["cards"] if c["card_id"] == "other")
    assert other["pricing"] == {"new_price": 0, "new_url": "search-other", "used_price": 99}
    assert out["generated_at"] == "2026-09-02"


def test_patch_manifest_reports_absent(tmp_path):
    manifest = {"generated_at": "x", "cards": [{"card_id": "onlyone", "pricing": {}}]}
    mp = tmp_path / "m.json"
    mp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    res = price_writeback.patch_manifest(mp, [{"slug": "notingrid", "new": 5, "url": AFF}])
    assert res["patched"] == [] and res["absent"] == ["notingrid"]


def test_result_feeds_build_site_new_cta(tmp_path):
    cards = _setup(tmp_path)
    sheet = _sheet({"slug": "canon-r5", "has_card": True, "needs_eyes": False,
                    "new": {"matched_by": "gtin", "price_usd": 3899.0, "affiliate_url": AFF}})
    changes, _, _ = price_writeback.plan(sheet, cards)
    price_writeback.apply_changes(changes, cards)
    card = json.loads((cards / "canon-r5.json").read_text())
    import build_site
    label, url = build_site.new_cta(card)
    assert label == "$3899 new"
    assert "prf.hn" in url
