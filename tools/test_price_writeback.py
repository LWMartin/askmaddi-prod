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


# ─── Withdrawal of stale feed prices (2026-11-04) ─────────────────────────────
def _feed_card(price=1999.0):
    return _card({"current_new_usd": price, "current_new_source": "adorama_feed",
                  "current_new_url": AFF, "affiliate_url": AFF})


def _cards(tmp_path, specs):
    """specs: {slug: card}. Returns the cards dir."""
    d = tmp_path / "cards"
    d.mkdir()
    for slug, card in specs.items():
        (d / f"{slug}.json").write_text(json.dumps(card, indent=2), encoding="utf-8")
    return d


def _gone(slug):
    return {"slug": slug, "has_card": True, "needs_eyes": False, "new": None}


def _load(d, slug):
    return json.loads((d / f"{slug}.json").read_text())


def test_withdraw_clears_only_the_price_on_a_feed_priced_card(tmp_path):
    d = _cards(tmp_path, {f"c{i}": _feed_card() for i in range(5)})
    w, why = price_writeback.plan_withdrawals(_sheet(_gone("c0")), d)
    assert why is None and [r["slug"] for r in w] == ["c0"]
    before = _load(d, "c0")
    price_writeback.apply_withdrawals(w, d)
    after = _load(d, "c0")
    assert after["pricing"]["current_new_usd"] is None
    for k in ("current_new_source", "current_new_url", "affiliate_url", "used_market"):
        assert after["pricing"][k] == before["pricing"][k]      # link + source + used kept
    assert {k: v for k, v in after.items() if k != "pricing"} == \
           {k: v for k, v in before.items() if k != "pricing"}


def test_withdraw_ignores_hand_set_and_already_withdrawn_prices(tmp_path):
    hand = _card({"current_new_usd": 500.0, "current_new_source": "manual"})
    gone = _feed_card(price=None)
    d = _cards(tmp_path, {"hand": hand, "gone": gone, "ok": _feed_card()})
    w, why = price_writeback.plan_withdrawals(_sheet(_gone("hand"), _gone("gone")), d)
    assert w == [] and why is None          # not ours / nothing to clear -> no churn


def test_withdraw_respects_exclude(tmp_path):
    d = _cards(tmp_path, {f"c{i}": _feed_card() for i in range(5)})
    w, _ = price_writeback.plan_withdrawals(_sheet(_gone("c0")), d, exclude={"c0"})
    assert w == []


def test_safety_wall_boundary(tmp_path):
    # 5 feed-priced cards: 1 withdrawal = 20% (allowed), 2 = 40% (all withheld).
    d = _cards(tmp_path, {f"c{i}": _feed_card() for i in range(5)})
    w, why = price_writeback.plan_withdrawals(_sheet(_gone("c0")), d)
    assert len(w) == 1 and why is None
    w, why = price_writeback.plan_withdrawals(_sheet(_gone("c0"), _gone("c1")), d)
    assert w == [] and "feed fault" in why


def test_withdrawn_card_renders_unpriced_cta_on_exact_product(tmp_path):
    import build_site
    d = _cards(tmp_path, {f"c{i}": _feed_card() for i in range(5)})
    w, _ = price_writeback.plan_withdrawals(_sheet(_gone("c0")), d)
    price_writeback.apply_withdrawals(w, d)
    label, url = build_site.new_cta(_load(d, "c0"))
    assert label == "Check price at Adorama" and url == AFF


def test_price_returns_the_next_night(tmp_path):
    d = _cards(tmp_path, {f"c{i}": _feed_card() for i in range(5)})
    w, _ = price_writeback.plan_withdrawals(_sheet(_gone("c0")), d)
    price_writeback.apply_withdrawals(w, d)
    back = _sheet({"slug": "c0", "has_card": True, "needs_eyes": False,
                   "new": {"matched_by": "gtin", "price_usd": 1899.0, "affiliate_url": AFF}})
    changes, _, _ = price_writeback.plan(back, d)
    price_writeback.apply_changes(changes, d)
    assert _load(d, "c0")["pricing"]["current_new_usd"] == 1899


def test_main_apply_withdraws_reports_and_patches_manifest(tmp_path, capsys):
    d = _cards(tmp_path, {f"c{i}": _feed_card() for i in range(5)})
    sheet = tmp_path / "sheet.json"
    sheet.write_text(json.dumps(_sheet(_gone("c0"))))
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"cards": [{"card_id": "c0", "pricing": {"new_price": 1999,
                                                                          "new_url": AFF}}]}))
    out = tmp_path / "changed.txt"
    price_writeback.main([str(sheet), "--cards-dir", str(d), "--apply",
                          "--manifest", str(manifest), "--changed-out", str(out)])
    assert "WITHDREW 1" in capsys.readouterr().out
    assert out.read_text().split() == ["c0"]
    entry = json.loads(manifest.read_text())["cards"][0]["pricing"]
    assert entry == {"new_price": None, "new_url": AFF}


def test_main_no_withdraw_flag(tmp_path):
    d = _cards(tmp_path, {f"c{i}": _feed_card() for i in range(5)})
    sheet = tmp_path / "sheet.json"
    sheet.write_text(json.dumps(_sheet(_gone("c0"))))
    price_writeback.main([str(sheet), "--cards-dir", str(d), "--apply", "--no-withdraw"])
    assert _load(d, "c0")["pricing"]["current_new_usd"] == 1999.0
