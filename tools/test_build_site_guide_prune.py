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


def test_rendered_guide_never_shows_a_delisted_card():
    """Real artifact: the street guide ranks 'Nikon 35mm f5' (a Nikon F5 film body
    minted as a lens, delisted 2026-11-05). After pruning, the rendered page must
    not name it, and the ranked count must drop by exactly one."""
    import json
    root = Path(__file__).resolve().parent.parent
    g = json.loads((root / "data" / "guides" / "cameras-for-street.json").read_text(encoding="utf-8"))
    ids = {r["card_id"] for r in g["ranked"]}
    assert "nikon-35mm-f5" in ids
    cards = {}
    for cid in ids - {"nikon-35mm-f5"}:
        cp = root / "data" / "cards" / f"{cid}.json"
        if cp.exists():
            cards[cid] = json.loads(cp.read_text(encoding="utf-8"))
    pruned = bs.prune_guides_to_cards([g], set(cards))[0]
    html = bs.render_guide(pruned, cards)
    assert "nikon-35mm-f5" not in html and "Nikon 35mm f5" not in html
    assert f"{len(cards)} ranked" in html
    assert [r["rank"] for r in pruned["ranked"]] == list(range(1, len(cards) + 1))


def test_every_card_list_is_pruned_and_counts_stay_exact():
    """Real artifact: lenses-for-landscape names 'Fujifilm 400mm f4.5' (a duplicate
    of the XF 100-400, delisted 2026-11-05) in its `excluded` ("didn't qualify")
    list — the list the first version of the prune missed."""
    import json
    root = Path(__file__).resolve().parent.parent
    g = json.loads((root / "data" / "guides" / "lenses-for-landscape.json").read_text(encoding="utf-8"))
    gone = {"fujifilm-400mm-f4-5", "canon-120-300mm-f2-8"}
    every = {e["card_id"] for k in ("ranked", "excluded", "pending_backfill") for e in g.get(k) or []}
    assert gone <= every
    p = bs.prune_guides_to_cards([g], every - gone)[0]
    for k in ("ranked", "excluded", "pending_backfill"):
        assert not ({e["card_id"] for e in p.get(k) or []} & gone), k
        assert p["counts"][k] == len(p.get(k) or []), k
    assert p["counts"]["in_scope"] == sum(len(p.get(k) or []) for k in ("ranked", "excluded", "pending_backfill"))
    assert p["counts"]["in_scope"] == g["counts"]["in_scope"] - len(gone & every)
    cards = {}
    for cid in every - gone:
        cp = root / "data" / "cards" / f"{cid}.json"
        if cp.exists():
            cards[cid] = json.loads(cp.read_text(encoding="utf-8"))
    html = bs.render_guide(p, cards)
    assert "Fujifilm 400mm f4.5" not in html and "Canon 120 300mm" not in html
