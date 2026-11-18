"""fix_wrong_listing: refuses unless the spine holds exactly the wrong values
named; dry run writes nothing; apply corrects the mpn THEN unbinds."""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'gateway'))
import fix_wrong_listing as f  # noqa: E402

REAL = Path(__file__).resolve().parent.parent / 'data' / 'skus.json'
ARGS = dict(slug='sony-a7s', expect_item='358393938452', reason='listing is the A7S II',
            mpn='ILCE7S/B', expect_mpn='ILCE7SM2/B')


def _spine(tmp_path):
    """The real sony-a7s entry, verbatim from data/skus.json."""
    entry = json.loads(REAL.read_text())['skus']['sony-a7s']
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
