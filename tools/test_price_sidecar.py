#!/usr/bin/env python3
"""Tests for tools/price_sidecar.py — the captured used-price store.

run: python3 -m pytest tools/test_price_sidecar.py

These pin the sidecar's contract: a gitignored, card_id-keyed store that
build_site overlays onto static cards. The whole point is that prices live HERE,
never in the tracked card spine — so these tests assert the read/write/overlay
behavior and the missing-file tolerance that lets a never-refreshed box build
cleanly.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import price_sidecar


def test_load_missing_file_returns_empty_not_error(tmp_path):
    # A box that has never refreshed has no sidecar — load must tolerate that
    # (first-run safe), returning an empty shape, NOT raising.
    s = price_sidecar.load(tmp_path / "nope.json")
    assert s["prices"] == {}
    assert "as_of" in s


def test_set_then_get_roundtrips(tmp_path):
    sc = tmp_path / "used_prices.json"
    um = {"source": "ebay", "bands": {"pre_owned": 697.0}, "sample_size": 31,
          "price_updated_at": "2026-06-24T10:10:04Z"}
    price_sidecar.set_used_market("sony-a7iv", um, path=sc)
    assert price_sidecar.get_used_market("sony-a7iv", path=sc) == um


def test_get_absent_card_returns_none(tmp_path):
    sc = tmp_path / "used_prices.json"
    price_sidecar.set_used_market("a", {"bands": {"x": 1}}, path=sc)
    assert price_sidecar.get_used_market("b", path=sc) is None


def test_set_multiple_cards_coexist(tmp_path):
    sc = tmp_path / "used_prices.json"
    price_sidecar.set_used_market("a", {"bands": {"x": 1}}, path=sc)
    price_sidecar.set_used_market("b", {"bands": {"y": 2}}, path=sc)
    # Second write must not clobber the first — read-modify-write, not overwrite.
    assert price_sidecar.get_used_market("a", path=sc)["bands"]["x"] == 1
    assert price_sidecar.get_used_market("b", path=sc)["bands"]["y"] == 2


def test_set_same_card_twice_updates_in_place(tmp_path):
    sc = tmp_path / "used_prices.json"
    price_sidecar.set_used_market("a", {"sample_size": 30}, path=sc)
    price_sidecar.set_used_market("a", {"sample_size": 31}, path=sc)
    assert price_sidecar.get_used_market("a", path=sc)["sample_size"] == 31
    # Still one entry, not two.
    assert list(price_sidecar.load(sc)["prices"].keys()) == ["a"]


def test_overlay_sets_used_market_on_card(tmp_path):
    sc = tmp_path / "used_prices.json"
    um = {"bands": {"pre_owned": 1450}}
    price_sidecar.set_used_market("sony-a7iv", um, path=sc)
    card = {"card_id": "sony-a7iv", "pricing": {"used_query": "Sony A7 IV"}}
    price_sidecar.overlay(card, path=sc)
    assert card["pricing"]["used_market"] == um
    assert card["pricing"]["used_query"] == "Sony A7 IV"  # other pricing survives


def test_overlay_absent_is_noop(tmp_path):
    sc = tmp_path / "used_prices.json"
    card = {"card_id": "ghost", "pricing": {}}
    price_sidecar.overlay(card, path=sc)
    assert "used_market" not in card["pricing"]


def test_overlay_card_without_id_is_safe(tmp_path):
    # A malformed card with no card_id must not crash the build; overlay skips it.
    sc = tmp_path / "used_prices.json"
    card = {"pricing": {}}
    price_sidecar.overlay(card, path=sc)
    assert "used_market" not in card["pricing"]


def test_corrupt_sidecar_raises(tmp_path):
    # A corrupt sidecar is a real error the caller should see — not silently
    # swallowed into an empty store (would mask data loss). Same discipline as
    # skus_registry.load_registry refusing a corrupt spine.
    sc = tmp_path / "used_prices.json"
    sc.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        price_sidecar.load(sc)


def test_atomic_write_leaves_no_temp_files(tmp_path):
    sc = tmp_path / "used_prices.json"
    price_sidecar.set_used_market("a", {"bands": {"x": 1}}, path=sc)
    # No .up-*.tmp turds left in the dir after a successful write.
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".up-")]
    assert leftovers == []
