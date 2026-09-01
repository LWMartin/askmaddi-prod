"""Offline tests for the eBay category sourcing tap — network + gate injected."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ebay_category_tap as tap  # noqa: E402


# ── fakes ────────────────────────────────────────────────────────────────────

def _fake_search(rows_by_query):
    calls = []
    def _search(query, limit=25, category_ids=None, condition_ids=None):
        calls.append({"q": query, "limit": limit,
                      "category_ids": category_ids, "condition_ids": condition_ids})
        return rows_by_query.get(query, [])
    _search.calls = calls
    return _search


def _fake_classify(kept_titles):
    """Classify a row as its facet only if its title is in kept_titles."""
    def _c(row):
        t = row.get("model", "")
        return {"category": "drone" if t in kept_titles else "accessory"}
    return _c


DRONE_ROWS = [
    {"item_id": "1", "title": "DJI Mavic 3 Pro Ready-to-Fly 4K Camera Drone", "brand": ""},
    {"item_id": "2", "title": "DJI Mavic 3 Pro ND Filter Set", "brand": "DJI"},   # accessory
    {"item_id": "3", "title": "Autel Robotics EVO Lite Drone", "brand": ""},
]

VERT = [{"vertical": "aerial", "facet": "drone", "gate_category": "drone",
         "ebay_category_id": "179697", "condition_ids": None,
         "seed_queries": ["dji mavic drone", "autel evo drone"]}]


# ── run(): screen, dedup, cap, param threading ───────────────────────────────

def test_run_keeps_only_gate_facet_and_drops_accessory():
    search = _fake_search({"dji mavic drone": DRONE_ROWS, "autel evo drone": []})
    classify = _fake_classify({"DJI Mavic 3 Pro Ready-to-Fly 4K Camera Drone",
                               "Autel Robotics EVO Lite Drone"})
    out = tap.run(VERT, search_fn=search, classify_fn=classify)
    slugs = {p["slug"] for p in out}
    assert any(s.startswith("dji-mavic-3-pro") for s in slugs)
    # the ND Filter accessory was screened out
    assert not any("filter" in s for s in slugs)
    assert all(p["fork_n"] == 0 and p["source"] == "ebay:drone" for p in out)


def test_run_threads_category_and_condition_params():
    search = _fake_search({"dji mavic drone": [], "autel evo drone": []})
    tap.run(VERT, search_fn=search, classify_fn=None)
    assert search.calls[0]["category_ids"] == "179697"
    assert search.calls[0]["condition_ids"] is None       # new + used


def test_run_dedups_vs_known():
    search = _fake_search({"dji mavic drone": DRONE_ROWS, "autel evo drone": []})
    classify = _fake_classify({"DJI Mavic 3 Pro Ready-to-Fly 4K Camera Drone"})
    first = tap.run(VERT, search_fn=search, classify_fn=classify)
    known = {p["slug"] for p in first}
    again = tap.run(VERT, search_fn=search, classify_fn=classify, known=known)
    assert again == []                                    # nothing new


def test_run_cap_limits_output():
    many = [{"item_id": str(i), "title": f"DJI Mavic {i} Pro Drone", "brand": "DJI"}
            for i in range(10)]
    search = _fake_search({"dji mavic drone": many, "autel evo drone": []})
    classify = _fake_classify({r["title"] for r in many})
    out = tap.run(VERT, search_fn=search, classify_fn=classify, cap=3)
    assert len(out) == 3


def test_run_no_classify_still_sinks_local_accessories():
    # Off-box (no gate) the LOCAL accessory sink still runs: the ND Filter row is
    # dropped, the two real drones pass.
    search = _fake_search({"dji mavic drone": DRONE_ROWS, "autel evo drone": []})
    out = tap.run(VERT, search_fn=search, classify_fn=None)
    slugs = {p["slug"] for p in out}
    assert not any("filter" in s for s in slugs)
    assert any(s.startswith("dji-mavic-3-pro") for s in slugs)
    assert any(s.startswith("autel-evo-lite") for s in slugs)


def test_local_accessory_sink_drops_batteries_and_parts():
    rows = [
        {"item_id": "b", "title": "Original DJI Avata 2 Intelligent Flight Batteries", "brand": "DJI"},
        {"item_id": "p", "title": "DJI Avata 2 Replacement Body Only Parts", "brand": "DJI"},
        {"item_id": "d", "title": "DJI Avata 2 Fly Smart USA In Stock", "brand": "DJI"},  # real drone
    ]
    search = _fake_search({"dji mavic drone": rows, "autel evo drone": []})
    out = tap.run(VERT, search_fn=search, classify_fn=None)   # gate off; local sink on
    slugs = {p["slug"] for p in out}
    assert slugs == {"dji-avata-2"}                           # only the aircraft


# ── brand-quality floor (drop-resurrection guard) ────────────────────────────

_FLOOR_ROWS = [
    {"item_id": "1", "title": "DJI Mini 4 Pro Camera Drone", "brand": ""},
    {"item_id": "2", "title": "SIMREX X900 Foldable GPS Drone", "brand": ""},   # no-name
    {"item_id": "3", "title": "Holy Stone HS720G GPS Drone", "brand": ""},      # multi-word brand
]
_VERT_FLOORED = [{"vertical": "aerial", "facet": "drone", "gate_category": "drone",
                  "ebay_category_id": "179697", "condition_ids": None,
                  "brand_floor": ["dji", "autel", "skydio", "holy stone"],
                  "seed_queries": ["mixed drones"]}]


def test_brand_floor_drops_no_name_keeps_recognized():
    """A no-name listing (SIMREX) slips the accessory sink + slug check but is
    below the brand floor; recognized brands (single- and multi-word) survive."""
    search = _fake_search({"mixed drones": _FLOOR_ROWS})
    out = tap.run(_VERT_FLOORED, search_fn=search, classify_fn=None)  # gate off; floor on
    vendors = {p["vendor"] for p in out}
    slugs = {p["slug"] for p in out}
    assert vendors == {"DJI", "Holy Stone"}
    assert not any("simrex" in s for s in slugs)   # the no-name was floored out


def test_absent_brand_floor_is_permissive():
    """No brand_floor key => no floor: the no-name passes (backward compat + the
    off-box/other-vertical path is unaffected)."""
    vert = [dict(_VERT_FLOORED[0])]
    vert[0].pop("brand_floor")
    search = _fake_search({"mixed drones": _FLOOR_ROWS})
    out = tap.run(vert, search_fn=search, classify_fn=None)
    assert any("simrex" in p["slug"] for p in out)


def test_passes_brand_floor_helper():
    floor = ["dji", "holy stone", "hover"]
    assert tap._passes_brand_floor("DJI", "DJI Mavic 3", floor)
    assert tap._passes_brand_floor("Mavic", "DJI Mavic 3 Pro", floor)   # title saves a mis-inferred brand
    assert tap._passes_brand_floor("", "Holy Stone HS720G", floor)      # multi-word brand
    assert not tap._passes_brand_floor("SIMREX", "SIMREX X900 Drone", floor)
    assert tap._passes_brand_floor("whatever", "no brands here", [])    # empty floor = permissive
    assert not tap._passes_brand_floor("", "Generic hovering quad", floor)  # \bhover\b != 'hovering'


def test_config_aerial_carries_brand_floor():
    """The shipped aerial vertical config actually enables the floor (guards
    against a future edit silently dropping it)."""
    cfg = json.loads((Path(__file__).resolve().parents[1]
                      / "data" / "ebay_source_verticals.json").read_text())
    aerial = next(v for v in cfg["verticals"] if v["vertical"] == "aerial")
    assert "dji" in aerial["brand_floor"] and "autel" in aerial["brand_floor"]


def test_default_slug_collapses_marketing_tail():
    assert tap.default_slug("DJI", "DJI Neo 2 * USA In Stock * 2-4 Shipping") == "dji-neo-2"
    assert tap.default_slug("DJI", "DJI Mini 5 Pro Fly More Combo (DJI RC 2)") == "dji-mini-5-pro"
    assert tap.default_slug("Autel", "Autel Robotics EVO Lite+ 6K Drone Bundle").startswith("autel-evo-lite")


# ── helpers ──────────────────────────────────────────────────────────────────

def test_guess_brand_from_title():
    assert tap._guess_brand("Autel Robotics EVO Lite Drone") == "Autel"
    assert tap._guess_brand("Holy Stone HS720R GPS Drone") == "Holy Stone"
    assert tap._guess_brand("HoverAir X1 Pro") == "HoverAir"


def test_default_slug_convention_and_dedupes_brand():
    s = tap.default_slug("DJI", "DJI Mavic 3 Pro Ready-to-Fly 4K Camera Drone (DJI RC)")
    assert s.startswith("dji-mavic-3-pro")
    assert "dji-dji" not in s                              # no doubled brand
    assert "ready" not in s and "camera" not in s          # noise stripped


def test_clean_model_strips_noise_and_parens():
    m = tap._clean_model("DJI Mavic 3 Pro Fly More Combo (DJI RC Pro) Ready-to-Fly 4K")
    assert "Fly More" not in m and "Ready" not in m and "(" not in m
    assert "Mavic 3 Pro" in m


# ── merge_into: union by slug ────────────────────────────────────────────────

def test_merge_into_unions_by_slug(tmp_path):
    p = tmp_path / "proposals.json"
    p.write_text(json.dumps([{"slug": "sony-a7-v", "fork_n": 0}]), encoding="utf-8")
    new = [{"slug": "sony-a7-v", "fork_n": 0},               # dup — skipped
           {"slug": "dji-mavic-3-pro", "fork_n": 0, "vendor": "DJI"}]
    added, total = tap.merge_into(p, new)
    assert added == 1 and total == 2
    slugs = {e["slug"] for e in json.loads(p.read_text())}
    assert slugs == {"sony-a7-v", "dji-mavic-3-pro"}
