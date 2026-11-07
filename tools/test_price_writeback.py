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
# Evidence rule: withdraw only when the card's OWN pinned product is absent or
# unavailable in the SAME-DAY snapshot; pinned-and-in-stock = matcher miss.
DAY = "2026-11-04"


def _aff(n):
    # Production-shaped pin (every real pin is adorama.com/<slug>/p/<code>), naming
    # the fixture card's product (display_name "Canon EOS R5"). Before the identity
    # gate (2026-11-06) this was `prod{n}.html`, which names no product: the gate
    # then rightly refuses it, and the end-to-end test only stayed green because
    # ALL fixture pins failed at once and the 20% safety wall withheld them —
    # a pass for the wrong reason. Unique per n, so snapshot matching is unchanged.
    return ("https://adorama.prf.hn/click/camref:x/destination:"
            f"https%3A%2F%2Fwww.adorama.com%2Fcanon-eos-r5-mirrorless-camera-body%2Fp%2Fcar5b{n}")


def _feed_card(n=0, price=1999.0):
    return _card({"current_new_usd": price, "current_new_source": "adorama_feed",
                  "current_new_url": _aff(n), "affiliate_url": _aff(n)})


def _cards(tmp_path, n=10, extra=None):
    d = tmp_path / "cards"
    d.mkdir()
    specs = {f"c{i}": _feed_card(i) for i in range(n)}
    specs.update(extra or {})
    for slug, card in specs.items():
        (d / f"{slug}.json").write_text(json.dumps(card, indent=2), encoding="utf-8")
    return d


def _snap(tmp_path, rows, day=DAY):
    sd = tmp_path / "snap"
    sd.mkdir(exist_ok=True)
    (sd / f"{day}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\ngarbage\n")
    return sd


def _row(n, stock="In Stock", cond="N"):
    return {"product_url": _aff(n), "stock_status": stock, "condition": cond, "price": 1}


def _gone(*slugs, day=DAY):
    return {"snapshot_date": day, "proposals": [
        {"slug": s, "has_card": True, "needs_eyes": False, "new": None} for s in slugs]}


def _load(d, slug):
    return json.loads((d / f"{slug}.json").read_text())


def test_reconstructs_2026_11_04_box_dry_run(tmp_path):
    # The real case: 2 pinned products In Stock (matcher missed them) must be KEPT,
    # 1 delisted + 1 out of stock withdrawn.
    d = _cards(tmp_path)
    sd = _snap(tmp_path, [_row(1, "In Stock"), _row(2, "In Stock"), _row(3, "Out of Stock")])
    w, why, misses = price_writeback.plan_withdrawals(_gone("c0", "c1", "c2", "c3"), d,
                                                      snapshot_dir=sd, max_fraction=0.5)
    assert why is None
    assert {r["slug"]: r["evidence"] for r in w} == {"c0": "not in feed",
                                                    "c3": "out of stock / not New"}
    assert sorted(misses) == ["c1", "c2"]


def test_listed_but_not_new_counts_as_unavailable(tmp_path):
    d = _cards(tmp_path)
    sd = _snap(tmp_path, [_row(0, "In Stock", cond="U")])
    w, _, misses = price_writeback.plan_withdrawals(_gone("c0"), d, snapshot_dir=sd)
    assert [r["slug"] for r in w] == ["c0"] and not misses


def test_fails_closed_without_same_day_evidence(tmp_path):
    d = _cards(tmp_path)
    for kwargs, sheet in (({}, _gone("c0")),                                   # no snapshot dir
                          ({"snapshot_dir": _snap(tmp_path, [], day="2026-11-03")}, _gone("c0")),
                          ({"snapshot_dir": tmp_path / "snap"},
                           {"proposals": _gone("c0")["proposals"]})):          # no snapshot_date
        w, why, _ = price_writeback.plan_withdrawals(sheet, d, **kwargs)
        assert w == [] and "none withdrawn" in why


def test_clears_only_the_price(tmp_path):
    d = _cards(tmp_path)
    w, _, _ = price_writeback.plan_withdrawals(_gone("c0"), d, snapshot_dir=_snap(tmp_path, []))
    before = _load(d, "c0")
    price_writeback.apply_withdrawals(w, d)
    after = _load(d, "c0")
    assert after["pricing"]["current_new_usd"] is None
    for k in ("current_new_source", "current_new_url", "affiliate_url", "used_market"):
        assert after["pricing"][k] == before["pricing"][k]
    assert {k: v for k, v in after.items() if k != "pricing"} == \
           {k: v for k, v in before.items() if k != "pricing"}


def test_ignores_hand_set_already_withdrawn_and_excluded(tmp_path):
    d = _cards(tmp_path, extra={"hand": _card({"current_new_usd": 500.0,
                                              "current_new_source": "manual"}),
                                "gone": _feed_card(99, price=None)})
    sd = _snap(tmp_path, [])
    w, why, m = price_writeback.plan_withdrawals(_gone("hand", "gone"), d, snapshot_dir=sd)
    assert (w, why, m) == ([], None, [])                 # nothing to do -> no churn
    w, _, _ = price_writeback.plan_withdrawals(_gone("c0"), d, snapshot_dir=sd, exclude={"c0"})
    assert w == []


def test_safety_wall_boundary(tmp_path):
    d = _cards(tmp_path, n=5)                            # 1/5 = 20% allowed, 2/5 withheld
    sd = _snap(tmp_path, [])
    w, why, _ = price_writeback.plan_withdrawals(_gone("c0"), d, snapshot_dir=sd)
    assert len(w) == 1 and why is None
    w, why, _ = price_writeback.plan_withdrawals(_gone("c0", "c1"), d, snapshot_dir=sd)
    assert w == [] and "feed fault" in why


def test_withdrawn_card_renders_unpriced_cta_on_exact_product(tmp_path):
    import build_site
    d = _cards(tmp_path)
    w, _, _ = price_writeback.plan_withdrawals(_gone("c0"), d, snapshot_dir=_snap(tmp_path, []))
    price_writeback.apply_withdrawals(w, d)
    assert build_site.new_cta(_load(d, "c0")) == ("Check price at Adorama", _aff(0))


def test_price_returns_the_next_night(tmp_path):
    d = _cards(tmp_path)
    w, _, _ = price_writeback.plan_withdrawals(_gone("c0"), d, snapshot_dir=_snap(tmp_path, []))
    price_writeback.apply_withdrawals(w, d)
    back = _sheet({"slug": "c0", "has_card": True, "needs_eyes": False,
                   "new": {"matched_by": "gtin", "price_usd": 1899.0, "affiliate_url": _aff(0)}})
    changes, _, _ = price_writeback.plan(back, d)
    price_writeback.apply_changes(changes, d)
    assert _load(d, "c0")["pricing"]["current_new_usd"] == 1899


def test_main_end_to_end(tmp_path, capsys):
    d = _cards(tmp_path)
    sd = _snap(tmp_path, [_row(1, "In Stock")])
    sheet = tmp_path / "sheet.json"
    sheet.write_text(json.dumps(_gone("c0", "c1")))
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"cards": [{"card_id": "c0", "pricing": {
        "new_price": 1999, "new_url": _aff(0)}}]}))
    out = tmp_path / "changed.txt"
    price_writeback.main([str(sheet), "--cards-dir", str(d), "--apply", "--snapshot-dir", str(sd),
                          "--manifest", str(manifest), "--changed-out", str(out)])
    text = capsys.readouterr().out
    assert "WITHDREW 1" in text and "matcher miss" in text and "c1" in text
    assert out.read_text().split() == ["c0"]
    assert _load(d, "c1")["pricing"]["current_new_usd"] == 1999.0     # kept
    assert json.loads(manifest.read_text())["cards"][0]["pricing"] == {"new_price": None,
                                                                       "new_url": _aff(0)}


def test_main_without_snapshot_withdraws_nothing(tmp_path, capsys):
    d = _cards(tmp_path)
    sheet = tmp_path / "sheet.json"
    sheet.write_text(json.dumps(_gone("c0")))
    price_writeback.main([str(sheet), "--cards-dir", str(d), "--apply"])
    assert _load(d, "c0")["pricing"]["current_new_usd"] == 1999.0
    assert "none withdrawn" in capsys.readouterr().out


# ─── identity gate (2026-11-06) — tools/pin_identity.py wired into the seat ───
# Real pins, copied from the cards (e92ea7d1): the GH5 card pinned to the GH5 II.
GH5_II = ("https://adorama.prf.hn/click/camref:x/destination:https%3A%2F%2Fwww.adorama.com"
          "%2Fpanasonic-lumix-gh5ii-mirrorless-camera-body-dc-gh5ii-4k-6k%2Fp%2Fipcdcgh5m2")
GH5 = ("https://adorama.prf.hn/click/camref:x/destination:https%3A%2F%2Fwww.adorama.com"
       "%2Fpanasonic-lumix-gh5-mirrorless-camera-body%2Fp%2Fipcdcgh5")


def _gh5_card(url=GH5_II, price=998.0):
    c = _card({"current_new_usd": price, "current_new_source": "adorama_feed",
               "current_new_url": url, "affiliate_url": url})
    c["identity"] = {"display_name": "Panasonic gh5", "model": "gh5"}
    return c


def _propose(slug, url, price=998.0, needs_eyes=False):
    return {"slug": slug, "has_card": True, "needs_eyes": needs_eyes,
            "new": {"matched_by": "mpn", "price_usd": price, "affiliate_url": url,
                    "title": None}}


def test_identity_failure_is_held_even_with_include_flagged(tmp_path):
    d = _cards(tmp_path, n=0, extra={"panasonic-gh5": _gh5_card(price=None)})
    changes, held, _ = price_writeback.plan(_sheet(_propose("panasonic-gh5", GH5_II)), d,
                                            include_flagged=True)
    assert changes == []
    assert held[0]["identity"] == ["generation: product is II, card is not"]


def test_passing_pin_still_applies(tmp_path):
    d = _cards(tmp_path, n=0, extra={"panasonic-gh5": _gh5_card(price=None)})
    changes, held, _ = price_writeback.plan(_sheet(_propose("panasonic-gh5", GH5)), d)
    assert [r["slug"] for r in changes] == ["panasonic-gh5"] and held == []


def test_existing_wrong_pin_is_withdrawn_not_frozen(tmp_path, capsys):
    """The trap a hold alone falls into: the sheet re-proposes the SAME wrong product
    every night, so 'held' would keep yesterday's wrong price on the card forever.
    End to end through main(): the price is cleared, link + source kept."""
    d = _cards(tmp_path, n=9, extra={"panasonic-gh5": _gh5_card()})   # 1 of 10 = 10%
    sheet = tmp_path / "sheet.json"
    sheet.write_text(json.dumps(_sheet(_propose("panasonic-gh5", GH5_II))))
    out = tmp_path / "changed.txt"
    price_writeback.main([str(sheet), "--cards-dir", str(d), "--apply",
                          "--changed-out", str(out)])
    text = capsys.readouterr().out
    p = _load(d, "panasonic-gh5")["pricing"]
    assert p["current_new_usd"] is None
    assert p["current_new_url"] == GH5_II and p["current_new_source"] == "adorama_feed"
    assert "HELD by identity gate" in text and "identity: generation" in text
    assert out.read_text().split() == ["panasonic-gh5"]
    assert all(_load(d, f"c{i}")["pricing"]["current_new_usd"] == 1999.0 for i in range(9))


def test_identity_withdrawal_skips_cards_getting_a_good_price_tonight(tmp_path):
    d = _cards(tmp_path, n=9, extra={"panasonic-gh5": _gh5_card()})
    w, why = price_writeback.plan_identity_withdrawals(d, keep={"panasonic-gh5"})
    assert (w, why) == ([], None)


def test_identity_withdrawal_ignores_hand_set_and_unpriced(tmp_path):
    hand = _gh5_card()
    hand["pricing"]["current_new_source"] = "manual"
    d = _cards(tmp_path, n=0, extra={"hand": hand, "unpriced": _gh5_card(price=None)})
    assert price_writeback.plan_identity_withdrawals(d) == ([], None)


def test_identity_withdrawal_safety_wall(tmp_path):
    """A rule fault that fails many pins at once withholds ALL (1/5 = 20% ok; 2/6 = 33% not)."""
    d = _cards(tmp_path, n=4, extra={"g1": _gh5_card()})
    w, why = price_writeback.plan_identity_withdrawals(d)
    assert [r["slug"] for r in w] == ["g1"] and why is None
    (d / "g2.json").write_text(json.dumps(_gh5_card(), indent=2), encoding="utf-8")
    w, why = price_writeback.plan_identity_withdrawals(d)
    assert w == [] and "rule fault" in why
