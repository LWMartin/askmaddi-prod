#!/usr/bin/env python3
"""Tests for tools/stamp_used_depth.py — run: python3 -m pytest tools/test_stamp_used_depth.py"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from stamp_used_depth import (
    build_query, is_target, is_fresh, run, CACHE_NAME,
)


def _row(brand, model, key, carded=False, mentioned=False):
    return {"brand": brand, "model": model, "key": key,
            "carded": carded, "mentioned": mentioned}


def _item(name, price, condition="Used", currency="USD"):
    return {"name": name, "price": str(price), "condition": condition,
            "currency": currency, "url": "https://www.ebay.com/itm/1"}


def _write_surface(tmp_path, covered):
    p = tmp_path / "demand-gated.json"
    p.write_text(json.dumps({"covered": covered}), encoding="utf-8")
    return p


def _fixture(dir_, key, items):
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / f"{key}.json").write_text(json.dumps({"items": items}),
                                      encoding="utf-8")


# ── query construction ───────────────────────────────────────────────────────
def test_build_query_dedupes_brand_prefix():
    # feed rows commonly double the brand into the model — don't repeat it
    assert build_query(_row("Hasselblad", "Hasselblad X1D Battery", "k")) \
        == "Hasselblad X1D Battery"


def test_build_query_joins_when_model_lacks_brand():
    assert build_query(_row("Nikon", "F3 HP", "k")) == "Nikon F3 HP"


def test_build_query_falls_back_to_present_field():
    assert build_query(_row("Nikon", "", "k")) == "Nikon"


def test_build_query_trims_generic_descriptors_keeps_model_number():
    # the RX100 V case that read depth 0 in live validation — descriptors go,
    # brand + model number stay
    q = build_query(_row("Sony", "Sony Cyber-shot DSC-RX100 V Digital Camera, Black", "k"))
    assert q == "Sony Cyber-shot DSC-RX100 V"


def test_build_query_never_empty_when_all_generic():
    # brand is always kept even if every model word is a descriptor
    assert build_query(_row("Sony", "Sony Digital Camera Black", "k")) == "Sony"


# ── target selection ─────────────────────────────────────────────────────────
def test_is_target_only_uncarded_unmentioned():
    assert is_target(_row("N", "F3", "k")) is True
    assert is_target(_row("N", "F3", "k", carded=True)) is False
    assert is_target(_row("N", "F3", "k", mentioned=True)) is False


# ── cache freshness ──────────────────────────────────────────────────────────
def test_is_fresh_window():
    now = datetime.now(timezone.utc)
    fresh = {"checked_at": (now - timedelta(days=5)).isoformat()}
    stale = {"checked_at": (now - timedelta(days=40)).isoformat()}
    assert is_fresh(fresh, now, 30) is True
    assert is_fresh(stale, now, 30) is False
    assert is_fresh({"checked_at": "garbage"}, now, 30) is False
    assert is_fresh({}, now, 30) is False


# ── end-to-end stamping (offline, via --from-json) ───────────────────────────
def test_run_stamps_depth_from_fixtures(tmp_path):
    covered = [_row("Nikon", "Nikon F3 HP", "mpn:nikon-f3")]
    surface = _write_surface(tmp_path, covered)
    fxdir = tmp_path / "fx"
    # 3 genuine used F3 HP listings (each carries ALL query tokens, so
    # listing_matches keeps them) -> clears default floor of 3
    _fixture(fxdir, "mpn:nikon-f3", [
        _item("Nikon F3 HP 35mm SLR Film Camera", 499.99),
        _item("Nikon F3 HP Eye Level SLR 35mm", 479.90),
        _item("Nikon F3 HP body clean tested", 460.00),
    ])
    stats = run(str(surface), "http://x", from_json=str(fxdir))

    data = json.loads(surface.read_text())
    assert data["covered"][0]["used_depth"] == 3
    assert stats["fetched"] == 1
    assert stats["cleared_default_floor"] == 1
    # cache written beside the surface
    cache = json.loads((tmp_path / CACHE_NAME).read_text())
    assert cache["mpn:nikon-f3"]["depth"] == 3


def test_run_skips_carded_and_mentioned(tmp_path):
    covered = [
        _row("Nikon", "Nikon F3", "k1", carded=True),
        _row("Canon", "Canon AE-1", "k2", mentioned=True),
    ]
    surface = _write_surface(tmp_path, covered)
    stats = run(str(surface), "http://x", from_json=str(tmp_path / "fx"))
    assert stats["targets"] == 0
    assert stats["fetched"] == 0
    data = json.loads(surface.read_text())
    assert "used_depth" not in data["covered"][0]
    assert "used_depth" not in data["covered"][1]


def test_run_honors_limit_and_defers(tmp_path):
    covered = [_row("B", f"B Model {i}", f"k{i}") for i in range(5)]
    surface = _write_surface(tmp_path, covered)
    fxdir = tmp_path / "fx"
    for i in range(5):
        _fixture(fxdir, f"k{i}", [_item(f"B Model {i} used", 100)])
    stats = run(str(surface), "http://x", limit=2, from_json=str(fxdir))
    assert stats["fetched"] == 2
    assert stats["deferred"] == 3


def test_run_second_pass_uses_cache_no_refetch(tmp_path):
    covered = [_row("Nikon", "Nikon F3", "mpn:nikon-f3")]
    surface = _write_surface(tmp_path, covered)
    fxdir = tmp_path / "fx"
    _fixture(fxdir, "mpn:nikon-f3", [_item("Nikon F3 used", 480)])
    run(str(surface), "http://x", from_json=str(fxdir))
    # second run with limit=0 must still stamp from cache
    stats = run(str(surface), "http://x", limit=0, from_json=str(fxdir))
    assert stats["from_cache"] == 1
    assert stats["fetched"] == 0
    assert json.loads(surface.read_text())["covered"][0]["used_depth"] == 1


def test_run_dry_run_writes_nothing(tmp_path):
    covered = [_row("Nikon", "Nikon F3", "mpn:nikon-f3")]
    surface = _write_surface(tmp_path, covered)
    fxdir = tmp_path / "fx"
    _fixture(fxdir, "mpn:nikon-f3", [_item("Nikon F3 used", 480)])
    run(str(surface), "http://x", from_json=str(fxdir), dry_run=True)
    # surface untouched, no cache file
    assert "used_depth" not in json.loads(surface.read_text())["covered"][0]
    assert not (tmp_path / CACHE_NAME).exists()


def test_run_below_floor_stamps_but_does_not_clear(tmp_path):
    covered = [_row("Odd", "Odd Widget", "k1")]
    surface = _write_surface(tmp_path, covered)
    fxdir = tmp_path / "fx"
    # only 1 genuine listing -> depth 1, below floor 3
    _fixture(fxdir, "k1", [_item("Odd Widget used", 50)])
    stats = run(str(surface), "http://x", from_json=str(fxdir))
    assert json.loads(surface.read_text())["covered"][0]["used_depth"] == 1
    assert stats["cleared_default_floor"] == 0
