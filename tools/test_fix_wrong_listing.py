"""fix_wrong_listing: refuses unless the spine holds exactly the wrong values
named; dry run writes nothing; apply corrects the mpn THEN unbinds."""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'gateway'))
import fix_wrong_listing as f  # noqa: E402

# The real entries as they stood BEFORE the corrections, frozen. Reading the
# live data/skus.json broke the bank gate on 2026-11-19: the correction this tool
# exists to make was applied on the box, so the live entry no longer held the
# wrong values and every nightly's bot_push gate failed here.
FROZEN = Path(__file__).resolve().parent / 'fixtures' / 'fix_wrong_listing_entries.json'
ARGS = dict(slug='sony-a7s', expect_item='358393938452', reason='listing is the A7S II',
            mpn='ILCE7S/B', expect_mpn='ILCE7SM2/B')


def _spine(tmp_path):
    """The real sony-a7s entry, verbatim, frozen before the correction."""
    entry = json.loads(FROZEN.read_text())['entries']['sony-a7s']
    p = tmp_path / 'skus.json'
    p.write_text(json.dumps({'version': '0.1.0', 'skus': {'sony-a7s': entry}}))
    return p


def test_dry_run_writes_nothing(tmp_path):
    p = _spine(tmp_path)
    before = p.read_text()
    assert f.run(path=p, **ARGS) == 0
    assert p.read_text() == before


def test_apply_on_the_real_entry(tmp_path):
    p = _spine(tmp_path)
    assert f.run(path=p, apply=True, **ARGS) == 0
    e = json.loads(p.read_text())['skus']['sony-a7s']
    assert e['identity']['mpn'] == 'ILCE7S/B'
    assert e['identity']['mpn_provenance']['corrected_from'] == 'ILCE7SM2/B'
    assert 'market_title' not in e['identity'] and 'image_catalog' not in e['identity']
    assert e['affiliate']['ebay_epn_url'] == ''
    assert e['unbound_listings'][0]['item_id'] == '358393938452'
    # second apply: the listing is gone -> refused, nothing rewritten
    before = p.read_text()
    assert f.run(path=p, apply=True, **ARGS) == 1
    assert p.read_text() == before


def test_refuses_a_different_listing_or_mpn(tmp_path):
    p = _spine(tmp_path)
    before = p.read_text()
    assert f.run(path=p, apply=True, **dict(ARGS, expect_item='1')) == 1
    assert f.run(path=p, apply=True, **dict(ARGS, expect_mpn='ILCE7SM3/B', mpn='X')) == 1
    assert p.read_text() == before


def test_missing_slug(tmp_path):
    p = _spine(tmp_path)
    assert f.run(path=p, **dict(ARGS, slug='ghost')) == 2


def test_plan_mode_runs_every_row_and_reports_the_worst(tmp_path):
    p = _spine(tmp_path)
    plan = tmp_path / 'plan.json'
    plan.write_text(json.dumps({'repoints': [
        dict(ARGS),
        dict(ARGS, slug='ghost')]}))
    before = p.read_text()
    assert f.run_plan(plan, path=p) == 2          # ghost: missing slug
    assert p.read_text() == before                # dry run
    assert f.run_plan(plan, apply=True, path=p) == 2
    e = json.loads(p.read_text())['skus']['sony-a7s']
    assert e['identity']['mpn'] == 'ILCE7S/B'     # the good row still applied


def test_real_repoint_plan_rows_are_well_formed():
    plan = Path(__file__).resolve().parent / 'rename_plans' / '2026-11-18-repoint.json'
    rows = json.loads(plan.read_text())['repoints']
    # Membership is checked against the spine as it was when the plan ran, not the
    # live one: a re-pointed card may later be delisted, and that must not fail
    # the bank gate.
    known = set(json.loads(FROZEN.read_text())['repoint_plan_slugs_in_spine'])
    for r in rows:
        assert {'slug', 'expect_item', 'expect_mpn', 'mpn', 'reason', 'new_name'} <= set(r)
        assert r['slug'] in known


# ── re-point clears the old new-price pin (2026-11-18) ───────────────────────
EF50 = ('https://adorama.prf.hn/click/camref:x/destination:https%3A%2F%2Fwww.adorama.com'
        '%2Fcanon-ef-50-mm-f-1-2l-usm-lens%2Fp%2Fca5012afu')
EF50_DEST = 'https://www.adorama.com/canon-ef-50-mm-f-1-2l-usm-lens/p/ca5012afu'


def _c50(tmp_path, pin=EF50):
    entry = json.loads(FROZEN.read_text())['entries']['canon-50mm-f1-2']
    p = tmp_path / 'skus.json'
    p.write_text(json.dumps({'version': '0.1.0', 'skus': {'canon-50mm-f1-2': entry}}))
    cards = tmp_path / 'cards'
    cards.mkdir()
    card = {'identity': {'display_name': 'Canon RF 50mm f/1.2L USM', 'model': '50mm f1.2'},
            'pricing': {'current_new_usd': 1349.0, 'current_new_url': pin, 'affiliate_url': pin,
                        'current_new_source': 'adorama_feed', 'used_market': {'bands': {'A': 1}}},
            'freshness': {'last_built': 'x'}}
    (cards / 'canon-50mm-f1-2.json').write_text(json.dumps(card, indent=2))
    item = entry['marketplace_ids']['ebay_legacy_item_id']
    args = dict(slug='canon-50mm-f1-2', expect_item=item, reason='reviews are the RF 50mm f/1.2L',
                mpn='2959C002', expect_mpn=entry['identity']['mpn'], expect_new_pin=EF50_DEST,
                cards_dir=cards)
    return p, cards / 'canon-50mm-f1-2.json', args


def test_repoint_clears_the_old_pin_and_nothing_else_on_the_card(tmp_path):
    p, cp, args = _c50(tmp_path)
    assert f.run(path=p, apply=True, **args) == 0
    pr = json.loads(cp.read_text())['pricing']
    assert pr['current_new_usd'] is None and pr['current_new_url'] is None
    assert pr['affiliate_url'] is None and pr['current_new_source'] is None
    assert pr['withdrawn_pin']['url'] == EF50 and pr['withdrawn_pin']['price'] == 1349.0
    assert pr['used_market'] == {'bands': {'A': 1}}
    assert json.loads(cp.read_text())['freshness'] == {'last_built': 'x'}
    assert json.loads(p.read_text())['skus']['canon-50mm-f1-2']['identity']['mpn'] == '2959C002'


def test_repoint_refuses_when_pinned_elsewhere_and_writes_nothing(tmp_path):
    other = EF50.replace('ca5012afu', 'ca5012zzz')
    p, cp, args = _c50(tmp_path, pin=other)
    spine0, card0 = p.read_text(), cp.read_text()
    assert f.run(path=p, apply=True, **args) == 1
    assert p.read_text() == spine0 and cp.read_text() == card0


def test_repoint_dry_run_names_the_pin_and_writes_nothing(tmp_path):
    p, cp, args = _c50(tmp_path)
    spine0, card0 = p.read_text(), cp.read_text()
    lines = []

    class W:
        def write(self, s): lines.append(s)
    assert f.run(path=p, out=W(), **args) == 0
    assert "would clear the card's new-price pin" in ''.join(lines)
    assert p.read_text() == spine0 and cp.read_text() == card0
