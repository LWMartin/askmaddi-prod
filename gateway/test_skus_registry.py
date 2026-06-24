"""
Unit tests for skus_registry — idempotent atomic skus.json writer.
==================================================================
Pins the maddi-skus-registry decision #3 properties: idempotency (re-resolve
is a no-op), atomic write, last-write-wins on genuine change. All offline,
tmp_path-isolated — no real data/skus.json touched.
"""
import json
import os

import skus_registry as reg


def _resolved(epid='15042899333', legacy='123456789012', mpn='ILCE-7M4',
              price='2498.00', title='Sony Alpha A7 IV'):
    return {
        'identity': {
            'epid': epid,
            'legacy_item_id': legacy,
            'ebay_category_id': '31388',
            'brand': 'Sony',
            'mpn': mpn,
            'market_title': title,
            'image': 'https://i.ebayimg.com/x.jpg',
            'price_seen': {'value': price, 'currency': 'USD', 'as_of': '2026-06-23T00:00:00Z'},
        },
        'affiliate_url': 'https://www.ebay.com/itm/123?campid=5339138080&customid=sony-a7iv',
        '_raw': {'big': 'payload'},
    }


def _entry(**kw):
    return reg.build_entry(
        slug='sony-a7iv', vendor='Sony', model='A7 IV', category='body',
        contamination_key='sony-a7-iv', resolved=_resolved(**kw),
    )


def test_load_missing_returns_empty(tmp_path):
    r = reg.load_registry(tmp_path / 'nope.json')
    assert r['skus'] == {}
    assert r['version'] == reg.SCHEMA_VERSION


def test_upsert_creates(tmp_path):
    p = tmp_path / 'skus.json'
    status = reg.upsert('sony-a7iv', _entry(), path=p)
    assert status == 'created'
    data = json.loads(p.read_text())
    assert 'sony-a7iv' in data['skus']
    assert data['skus']['sony-a7iv']['identity']['epid'] == '15042899333'
    assert data['skus']['sony-a7iv']['contamination_key'] == 'sony-a7-iv'


def test_upsert_idempotent_second_call_unchanged(tmp_path):
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _entry(), path=p)
    mtime1 = os.path.getmtime(p)
    # Re-resolve the same item (price_seen/resolved_at differ, identity same).
    status = reg.upsert('sony-a7iv', _entry(price='2599.00'), path=p)
    assert status == 'unchanged'
    # File not rewritten -> mtime stable, no churn.
    assert os.path.getmtime(p) == mtime1


def test_upsert_updates_on_identity_change(tmp_path):
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _entry(), path=p)
    # epid genuinely changed -> the item is different, last-write-wins.
    status = reg.upsert('sony-a7iv', _entry(epid='99999999999'), path=p)
    assert status == 'updated'
    data = json.loads(p.read_text())
    assert data['skus']['sony-a7iv']['identity']['epid'] == '99999999999'


def test_upsert_two_skus_coexist(tmp_path):
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _entry(), path=p)
    lens = reg.build_entry(
        slug='sigma-35-art-dg-dn-ii', vendor='Sigma', model='35mm f/1.4 DG DN Art II',
        category='lens', contamination_key='sigma-35-art-dg-dn-ii',
        resolved=_resolved(epid='22222', legacy='888', mpn='340969', title='Sigma 35'),
    )
    reg.upsert('sigma-35-art-dg-dn-ii', lens, path=p)
    data = json.loads(p.read_text())
    assert set(data['skus'].keys()) == {'sony-a7iv', 'sigma-35-art-dg-dn-ii'}


def test_atomic_write_leaves_no_temp_files(tmp_path):
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _entry(), path=p)
    leftovers = [f for f in os.listdir(tmp_path) if f.startswith('.skus-')]
    assert leftovers == []


def test_written_json_is_valid_and_reloadable(tmp_path):
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _entry(), path=p)
    # Round-trips through load_registry cleanly.
    r = reg.load_registry(p)
    assert r['skus']['sony-a7iv']['identity']['mpn'] == 'ILCE-7M4'


def test_build_entry_drops_raw_from_persisted_entry(tmp_path):
    e = _entry()
    # _raw must not leak into the persisted entry (large; schema captures need).
    assert '_raw' not in e
    assert '_raw' not in e['identity']
    assert e['affiliate']['ebay_epn_url'].startswith('https://www.ebay.com/itm/')
    assert e['affiliate']['amazon_asin'] is None
