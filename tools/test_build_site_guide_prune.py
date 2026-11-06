"""prune_guides_to_cards: a guide ranks only cards that exist (2026-11-05)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_site as bs  # noqa: E402


def _guide():
    return {"id": "cameras-for-street", "display_name": "Street", "applies_to": ["body"],
            "criteria": [{"display": "Autofocus"}],
            "ranked": [{"card_id": "sony-a7iv", "rank": 1, "display_name": "Sony A7 IV"},
                       {"card_id": "nikon-35mm-f5", "rank": 2, "display_name": "Nikon 35mm f5"},
                       {"card_id": "fujifilm-x-t5", "rank": 3, "display_name": "Fujifilm X-T5"}]}


def test_drops_missing_cards_and_renumbers():
    g = _guide()
    out = bs.prune_guides_to_cards([g], {"sony-a7iv", "fujifilm-x-t5"})
    assert [(r["card_id"], r["rank"]) for r in out[0]["ranked"]] == [("sony-a7iv", 1), ("fujifilm-x-t5", 2)]
    assert len(g["ranked"]) == 3, "input guide must not be mutated"


def test_untouched_guide_is_returned_as_is():
    g = _guide()
    out = bs.prune_guides_to_cards([g], {"sony-a7iv", "nikon-35mm-f5", "fujifilm-x-t5"})
    assert out[0] is g


# ─── real-artifact fixtures ──────────────────────────────────────────────────
# These were first written (2026-11-05) to read the LIVE data/guides/*.json and
# assert the delisted cards were still in it. That artifact is pipeline-owned and
# regenerated nightly by regen_guides.py (askmaddi cron, 09:50 UTC), which drops a
# delisted id at the source — so the precondition went false on the very next
# regen, the 2026-11-06 nightly gate failed, and bot_push refused to bank the
# night (prices + pages left as /opt dirt). A test must not bind to the transient
# content of an artifact the pipeline rewrites.
#
# So the real case is PINNED: verbatim slices of the two guide artifacts as banked
# at askmaddi-prod 9af2e314 (the last tree before the regen dropped the ids) —
# real card_ids, display_names, ranks, reasons and unknowns, each delisted entry
# kept with its real neighbours. Trimmed to identity fields (rationale/flags are
# not read by the prune nor by these assertions); `counts` recomputed for the
# slice so the artifact invariant (count == list length, in_scope == sum) holds,
# exactly as prune_guides_to_cards maintains it. No live data/ is read.

def _street_0511():
    """cameras-for-street @ 9af2e314: ranked[53:56] + ranked[87:90]. Two cards
    delisted 2026-11-05 sit in it: 'Sony 35mm f3' (rank 55) and 'Nikon 35mm f5'
    (rank 89, a Nikon F5 film body minted as a lens)."""
    ranked = [
        {"card_id": "panasonic-lumix-gh6", "display_name": "Panasonic lumix gh6", "rank": 54},
        {"card_id": "sony-35mm-f3", "display_name": "Sony 35mm f3", "rank": 55},
        {"card_id": "om-system-om-5", "display_name": "OM SYSTEM om 5", "rank": 56},
        {"card_id": "canon-eos-r8-ii", "display_name": "Canon eos r8 ii", "rank": 88},
        {"card_id": "nikon-35mm-f5", "display_name": "Nikon 35mm f5", "rank": 89},
        {"card_id": "sony-a6300", "display_name": "Sony a6300", "rank": 90},
    ]
    return {"id": "cameras-for-street", "display_name": "Street Photography",
            "applies_to": ["body"],
            "criteria": [{"axis": "handling", "weight": 3, "display": "Handling"},
                         {"axis": "autofocus_body", "weight": 2, "display": "Autofocus"}],
            "ranked": ranked, "excluded": [], "pending_backfill": [],
            "counts": {"in_scope": 6, "ranked": 6, "excluded": 0, "pending_backfill": 0}}


def _landscape_0511():
    """lenses-for-landscape @ 9af2e314: ranked[0:2], excluded[2:5] + [22:25], and
    the whole pending_backfill. Two cards delisted 2026-11-05 sit in `excluded`
    ("didn't qualify") — the list the first version of the prune missed:
    'Canon 120 300mm f2.8' and 'Fujifilm 400mm f4.5' (a duplicate of the XF
    100-400)."""
    ranked = [
        {"card_id": "fujifilm-23mm-f1-4", "display_name": "Fujifilm 23mm f1.4", "rank": 1},
        {"card_id": "sigma-35-art-dg-dn-ii", "display_name": "Sigma 35mm f/1.4 DG DN Art II", "rank": 2},
    ]
    excluded = [
        {"card_id": "canon-100mm-f2", "display_name": "Canon 100mm f2",
         "reasons": ["require focal_length_mm <= 35: got 100"]},
        {"card_id": "canon-120-300mm-f2-8", "display_name": "Canon 120 300mm f2.8",
         "reasons": ["require focal_length_mm <= 35: got 300"]},
        {"card_id": "canon-135mm-f2-0", "display_name": "Canon 135mm f2.0",
         "reasons": ["require focal_length_mm <= 35: got 135"]},
        {"card_id": "fujifilm-16-55mm-f2-8", "display_name": "Fujifilm 16 55mm f2.8",
         "reasons": ["require focal_length_mm <= 35: got 55"]},
        {"card_id": "fujifilm-400mm-f4-5", "display_name": "Fujifilm 400mm f4.5",
         "reasons": ["require focal_length_mm <= 35: got 400"]},
        {"card_id": "fujifilm-50-140mm-f2-8", "display_name": "Fujifilm 50 140mm f2.8",
         "reasons": ["require focal_length_mm <= 35: got 140"]},
    ]
    pending = [{"card_id": "nikon-z-20", "display_name": "Nikon z 20",
                "unknowns": ["focal_length_mm: focal_length_mm not present on card"]}]
    return {"id": "lenses-for-landscape", "display_name": "Landscape",
            "applies_to": ["lens"],
            "criteria": [{"axis": "optical_performance", "weight": 3, "display": "Optical Performance"},
                         {"axis": "distortion", "weight": 2, "display": "Distortion"}],
            "ranked": ranked, "excluded": excluded, "pending_backfill": pending,
            "counts": {"in_scope": 9, "ranked": 2, "excluded": 6, "pending_backfill": 1}}


def _all_ids(g):
    return {e["card_id"] for k in ("ranked", "excluded", "pending_backfill") for e in g.get(k) or []}


def test_rendered_guide_never_shows_a_delisted_card():
    """Real case (street @ 9af2e314): after pruning to the cards that exist, the
    rendered page names neither delisted card, ranks renumber 1..n, and the page's
    ranked count drops by exactly the number delisted."""
    g = _street_0511()
    gone = {"sony-35mm-f3", "nikon-35mm-f5"}
    assert gone <= _all_ids(g), "fixture must carry the real delisted entries"
    live = _all_ids(g) - gone
    pruned = bs.prune_guides_to_cards([g], live)[0]
    html = bs.render_guide(pruned, {})
    for cid, name in (("sony-35mm-f3", "Sony 35mm f3"), ("nikon-35mm-f5", "Nikon 35mm f5")):
        assert cid not in html and name not in html, cid
    assert [r["card_id"] for r in pruned["ranked"]] == [
        "panasonic-lumix-gh6", "om-system-om-5", "canon-eos-r8-ii", "sony-a6300"]
    assert [r["rank"] for r in pruned["ranked"]] == [1, 2, 3, 4]
    assert "4 ranked" in html and "6 ranked" not in html
    assert pruned["counts"] == {"in_scope": 4, "ranked": 4, "excluded": 0, "pending_backfill": 0}
    assert len(g["ranked"]) == 6, "input guide must not be mutated"


def test_every_card_list_is_pruned_and_counts_stay_exact():
    """Real case (landscape @ 9af2e314): both delisted cards live in `excluded`.
    Every card list is pruned, untouched lists keep their entries, counts stay
    exact, and the rendered page names neither."""
    g = _landscape_0511()
    gone = {"fujifilm-400mm-f4-5", "canon-120-300mm-f2-8"}
    assert gone <= {e["card_id"] for e in g["excluded"]}, "fixture must carry the real delisted entries"
    p = bs.prune_guides_to_cards([g], _all_ids(g) - gone)[0]
    for k in ("ranked", "excluded", "pending_backfill"):
        assert not ({e["card_id"] for e in p.get(k) or []} & gone), k
        assert p["counts"][k] == len(p.get(k) or []), k
    assert [e["card_id"] for e in p["ranked"]] == ["fujifilm-23mm-f1-4", "sigma-35-art-dg-dn-ii"]
    assert [e["card_id"] for e in p["pending_backfill"]] == ["nikon-z-20"]
    assert len(p["excluded"]) == 4
    assert p["counts"]["in_scope"] == sum(len(p.get(k) or []) for k in ("ranked", "excluded", "pending_backfill"))
    assert p["counts"]["in_scope"] == g["counts"]["in_scope"] - len(gone) == 7
    html = bs.render_guide(p, {})
    assert "Fujifilm 400mm f4.5" not in html and "Canon 120 300mm" not in html


def test_guide_tests_read_no_live_data(monkeypatch):
    """Guard for the class that failed the 2026-11-06 nightly: the real-case tests
    must not depend on the repo's live data/ (guides regenerate nightly, cards
    churn). Behavioral, not a source scan: every file open under data/ raises
    while the tests run, so a read built any way at all is caught."""
    import builtins
    import io
    live = (Path(__file__).resolve().parent.parent / "data").resolve()

    def _is_live(p):
        try:
            return Path(p).resolve().is_relative_to(live)
        except (TypeError, ValueError, AttributeError):
            return False

    def _deny(orig):
        def wrapped(file, *a, **k):
            if _is_live(file):
                raise AssertionError(f"live data read: {file}")
            return orig(file, *a, **k)
        return wrapped

    monkeypatch.setattr(builtins, "open", _deny(builtins.open))
    monkeypatch.setattr(io, "open", _deny(io.open))
    test_rendered_guide_never_shows_a_delisted_card()
    test_every_card_list_is_pruned_and_counts_stay_exact()
