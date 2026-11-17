"""Tests for tools/rename_cards.py (2026-11-17).

Synthetic cards in a temp dir; card_export is a temp file holding the same
function shape (loaded by path, as on the box), so no test depends on a sibling
phantom-ops checkout and none can silently skip."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rename_cards import load_card_export, plan_row, run  # noqa: E402

CARD_EXPORT_SRC = '''
def build_export_artifacts(card, *, slug, base_url="https://askmaddi.com"):
    name = (card.get("identity") or {}).get("display_name") or ""
    para = ((card.get("synthesis") or {}).get("consensus_paragraph") or "").strip()
    link = f"{base_url}/cards/{slug}/"
    return {"permalink": link, "reddit": f"{name}\\n\\n{para}\\n\\n{link}",
            "discord": f"**{name}**\\n{para}", "og": {"title": f"{name} - Reviewed", "url": link}}
'''

OLD, NEW = "Canon 100 400mm f5.6", "Canon RF 100-400mm f/5.6-8 IS USM"
ROW = {"slug": "canon-100-400mm-f5-6", "old": OLD, "new": NEW}


def _export_fn(tmp_path):
    (tmp_path / "card_export.py").write_text(CARD_EXPORT_SRC)
    return load_card_export(tmp_path)


def _card(build, name=OLD, para=None, pricing=None):
    card = {"card_id": ROW["slug"], "identity": {"display_name": name},
            "synthesis": {"consensus_paragraph": para if para is not None else
                          f"Synthesized from 20 reviewer sources for {name}. Optics lead.",
                          "aspect_breakdown": [{"top_quote_refs": [
                              {"quote_excerpt": f"the {OLD} is sharp"}]}]},
            "sources": [{"title": f"{OLD} review"}], "pricing": pricing or {}}
    card["export"] = build(card, slug=ROW["slug"])
    return card


SPINE = {"overrides": {}}


def test_happy_path_renames_name_sentence_and_export(tmp_path):
    build = _export_fn(tmp_path)
    status, why, out = plan_row(ROW, _card(build), SPINE, build)
    assert (status, why) == ("ok", "")
    assert out["identity"]["display_name"] == NEW
    assert out["synthesis"]["consensus_paragraph"].startswith(
        f"Synthesized from 20 reviewer sources for {NEW}.")
    assert out["export"] == build(out, slug=ROW["slug"])
    # reviewers' words untouched
    assert out["sources"][0]["title"] == f"{OLD} review"
    assert OLD in out["synthesis"]["aspect_breakdown"][0]["top_quote_refs"][0]["quote_excerpt"]


def test_live_name_changed_since_review_is_refused(tmp_path):
    build = _export_fn(tmp_path)
    status, why, _ = plan_row(ROW, _card(build, name="Canon something else"), SPINE, build)
    assert status == "refused" and "live name" in why


def test_unreproducible_export_is_refused(tmp_path):
    build = _export_fn(tmp_path)
    card = _card(build)
    card["export"]["og"]["title"] = "hand edited"
    status, why, _ = plan_row(ROW, card, SPINE, build)
    assert status == "refused" and "reproduce" in why


def test_rename_that_breaks_a_live_price_pin_is_refused(tmp_path):
    build = _export_fn(tmp_path)
    row = {"slug": "s", "old": "Sony 70 200mm f2.8", "new": "Sony FE 70-200mm f/2.8 GM OSS II"}
    url = ("https://adorama.prf.hn/click/camref:x/destination:https%3A%2F%2Fwww.adorama.com"
           "%2Fsony-fe-70-200-mm-f-2-8-gm-oss-lens%2Fp%2Fiso70200gm")
    card = _card(build, name=row["old"], pricing={"current_new_source": "adorama_feed",
                                                  "current_new_usd": 1999, "current_new_url": url})
    card["export"] = build(card, slug="s")
    status, why, _ = plan_row(row, card, SPINE, build)
    assert status == "refused" and "identity gate" in why


def test_old_name_outside_the_template_sentence_is_refused(tmp_path):
    build = _export_fn(tmp_path)
    card = _card(build, para=f"Reviewers call the {OLD} sharp.")
    status, why, _ = plan_row(ROW, card, SPINE, build)
    assert status == "refused" and "consensus" in why


def test_single_source_anchor_uses_singular(tmp_path):
    build = _export_fn(tmp_path)
    card = _card(build, para=f"Synthesized from 1 reviewer source for {OLD}. Thin.")
    status, _, out = plan_row(ROW, card, SPINE, build)
    assert status == "ok"
    assert out["synthesis"]["consensus_paragraph"] == f"Synthesized from 1 reviewer source for {NEW}. Thin."


def test_missing_spine_entry_is_refused(tmp_path):
    build = _export_fn(tmp_path)
    status, why, _ = plan_row(ROW, _card(build), None, build)
    assert status == "refused" and "spine" in why


def test_apply_writes_spine_and_card_then_rerun_is_already(tmp_path):
    (tmp_path / "card_export.py").write_text(CARD_EXPORT_SRC)
    build = load_card_export(tmp_path)
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / f"{ROW['slug']}.json").write_text(json.dumps(_card(build), indent=2))
    spine = tmp_path / "skus.json"
    spine.write_text(json.dumps({"version": 1, "skus": {ROW["slug"]: {"vendor": "Canon"}}}))
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"renames": [ROW]}))
    sink = open(tmp_path / "log.txt", "w")

    dry = run(plan, cards, spine, tmp_path, apply=False, out=sink)
    assert dry["ok"] == 1 and dry["written"] == 0
    assert json.loads((cards / f"{ROW['slug']}.json").read_text())["identity"]["display_name"] == OLD

    c = run(plan, cards, spine, tmp_path, apply=True, out=sink)
    assert c["written"] == 1
    assert json.loads(spine.read_text())["skus"][ROW["slug"]]["overrides"]["display_name"] == NEW
    assert json.loads((cards / f"{ROW['slug']}.json").read_text())["identity"]["display_name"] == NEW

    again = run(plan, cards, spine, tmp_path, apply=True, out=sink)
    assert again["already"] == 1 and again["written"] == 0


def test_old_name_left_after_the_template_swap_is_refused(tmp_path):
    build = _export_fn(tmp_path)
    card = _card(build, para=f"Synthesized from 20 reviewer sources for {OLD}. The {OLD} wins.")
    status, why, _ = plan_row(ROW, card, SPINE, build)
    assert status == "refused" and "survives" in why
