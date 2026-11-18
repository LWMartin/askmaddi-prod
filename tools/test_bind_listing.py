"""bind_listing: binds only on a matching part number, never on silence; the
writer is compare-and-swap on 'unbound' and never touches authored fields."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'gateway'))

import bind_listing as bl  # noqa: E402
import skus_registry as sr  # noqa: E402


class FakeEbay:
    def __init__(self, by_query, items):
        self.by_query, self.items = by_query, items
        self.searches, self.resolves = [], []

    def search_candidates(self, query, limit=10):
        self.searches.append(query)
        return self.by_query.get(query, [])

    def resolve(self, item_id, customid=None):
        self.resolves.append(item_id)
        return self.items[item_id]


def _cand(legacy, title):
    return {'item_id': f'v1|{legacy}|0', 'title': title, 'epid': ''}


def _item(legacy, title, mpn, brand='Nikon', cat=''):
    return {'identity': {'legacy_item_id': legacy, 'epid': 'e' + legacy, 'brand': brand,
                         'mpn': mpn, 'market_title': title, 'image': f'https://img/{legacy}.jpg',
                         'image_catalog': cat,
                         'price_seen': {'value': '500.00', 'currency': 'USD', 'as_of': 't'}},
            'affiliate_url': f'https://ebay/itm/{legacy}?epn'}


def _spine(tmp_path, **over):
    entry = {
        'vendor': 'Nikon', 'model': '24mm f1.8', 'facet': 'lens',
        'identity': {'brand': 'Nikon', 'mpn': '20080',
                     'mpn_provenance': {'method': 'wrong-listing-correction'},
                     'image_catalog': ''},
        'marketplace_ids': {'ebay_legacy_item_id': '', 'ebay_epid': ''},
        'affiliate': {'ebay_epn_url': ''},
        'overrides': {'display_name': 'Nikon NIKKOR Z 24mm f/1.8 S'},
        'unbound_listings': [{'item_id': '111', 'market_title': 'Nikon AF-S 24mm f/1.8G'}],
    }
    entry.update(over)
    p = tmp_path / 'skus.json'
    p.write_text(json.dumps({'skus': {'nikon-24mm-f1-8': entry,
                                      'bound-card': {'identity': {'mpn': 'X1'},
                                                     'marketplace_ids': {'ebay_legacy_item_id': '9'},
                                                     'unbound_listings': [{'item_id': '8'}]},
                                      'never-bound': {'identity': {'mpn': 'X2'},
                                                      'marketplace_ids': {'ebay_legacy_item_id': ''}}}}))
    return p


Q1, Q2 = 'Nikon 20080', 'Nikon NIKKOR Z 24mm f/1.8 S'


def test_silent_part_number_never_binds_but_a_matching_one_does(tmp_path):
    # The case the door exists for: a used F-mount 24mm f/1.8G that omits its
    # part number must not bind onto the Z card; the Z listing that carries
    # 20080 does.
    p = _spine(tmp_path)
    ebay = FakeEbay({Q1: [_cand('201', 'Nikon 24mm f/1.8 lens'), _cand('202', 'Nikon Z 24mm S')]},
                    {'v1|201|0': _item('201', 'Nikon AF-S 24mm f/1.8G ED', ''),
                     'v1|202|0': _item('202', 'Nikon NIKKOR Z 24mm f/1.8 S', '20080')})
    bl.run(slugs=['nikon-24mm-f1-8'], commit=True, ebay=ebay, path=p, sleep_s=0, out=lambda *_: None)
    e = sr.load_registry(p)['skus']['nikon-24mm-f1-8']
    assert e['marketplace_ids']['ebay_legacy_item_id'] == '202'
    assert e['identity']['market_title'] == 'Nikon NIKKOR Z 24mm f/1.8 S'
    assert e['affiliate']['ebay_epn_url'] == 'https://ebay/itm/202?epn'
    assert e['identity']['mpn'] == '20080' and e['identity']['mpn_provenance']
    assert e['overrides'] == {'display_name': 'Nikon NIKKOR Z 24mm f/1.8 S'}
    assert e['bound_listings'][0]['item_id'] == '202'
    assert e['bound_listings'][0]['method'] == 'part-number-match'


def test_a_different_part_number_is_refused(tmp_path):
    p = _spine(tmp_path)
    ebay = FakeEbay({Q1: [_cand('203', 'Nikon 24mm')]},
                    {'v1|203|0': _item('203', 'Nikon AF-S 24mm f/1.8G ED', '2215')})
    r = bl.find('nikon-24mm-f1-8', sr.load_registry(p)['skus']['nikon-24mm-f1-8'],
                ebay=ebay, sleep_s=0)
    assert r['verdict'] == 'NOT-FOUND'
    assert "is not '20080'" in r['inspected'][0]['reason']


def test_previously_unbound_listing_is_skipped_without_a_lookup(tmp_path):
    p = _spine(tmp_path)
    ebay = FakeEbay({Q1: [_cand('111', 'Nikon AF-S 24mm f/1.8G 20080')]},
                    {'v1|111|0': _item('111', 'Nikon AF-S 24mm f/1.8G', '20080')})
    r = bl.find('nikon-24mm-f1-8', sr.load_registry(p)['skus']['nikon-24mm-f1-8'],
                ebay=ebay, sleep_s=0)
    assert r['verdict'] == 'NOT-FOUND' and ebay.resolves == []


def test_name_search_runs_only_when_the_part_number_finds_nothing_acceptable(tmp_path):
    p = _spine(tmp_path)
    ebay = FakeEbay({Q1: [], Q2: [_cand('204', 'Nikon NIKKOR Z 24mm f/1.8 S')]},
                    {'v1|204|0': _item('204', 'Nikon NIKKOR Z 24mm f/1.8 S', '20080')})
    r = bl.find('nikon-24mm-f1-8', sr.load_registry(p)['skus']['nikon-24mm-f1-8'],
                ebay=ebay, sleep_s=0)
    assert ebay.searches == [Q1, Q2] and r['verdict'] == 'FOUND' and r['query'] == Q2
    ebay2 = FakeEbay({Q1: [_cand('202', 'Z 24 20080')]},
                     {'v1|202|0': _item('202', 'Nikon NIKKOR Z 24mm f/1.8 S', '20080')})
    bl.find('nikon-24mm-f1-8', sr.load_registry(p)['skus']['nikon-24mm-f1-8'], ebay=ebay2, sleep_s=0)
    assert ebay2.searches == [Q1]


def test_firewall_still_refuses_an_accessory_for_the_product(tmp_path):
    # A matching part number does not excuse a "for <product>" listing.
    p = _spine(tmp_path)
    ebay = FakeEbay({Q1: [_cand('205', 'Lens hood for Nikon Z 24mm 20080')]},
                    {'v1|205|0': _item('205', 'Lens Hood for Nikon Z 24mm f/1.8 S 20080', '20080')})
    r = bl.find('nikon-24mm-f1-8', sr.load_registry(p)['skus']['nikon-24mm-f1-8'],
                ebay=ebay, sleep_s=0)
    assert r['verdict'] == 'NOT-FOUND'
    assert r['inspected'][0]['reason'].startswith('firewall:')


def test_other_brand_is_refused(tmp_path):
    p = _spine(tmp_path)
    ebay = FakeEbay({Q1: [_cand('206', 'x')]},
                    {'v1|206|0': _item('206', 'Viltrox 24mm', '20080', brand='Viltrox')})
    r = bl.find('nikon-24mm-f1-8', sr.load_registry(p)['skus']['nikon-24mm-f1-8'],
                ebay=ebay, sleep_s=0)
    assert r['verdict'] == 'NOT-FOUND' and 'brand' in r['inspected'][0]['reason']


def test_selector_takes_only_unbound_entries_with_receipts(tmp_path):
    skus = sr.load_registry(_spine(tmp_path))['skus']
    assert bl.selected(skus, all_unbound=True)[0] == ['nikon-24mm-f1-8']


def test_writer_is_compare_and_swap_and_keeps_a_catalog_photo(tmp_path):
    p = _spine(tmp_path)
    assert sr.bind_listing('bound-card', _item('7', 't', 'X1'), {}, path=p) == 'already-bound'
    assert sr.bind_listing('nikon-24mm-f1-8', _item('111', 't', '20080'), {}, path=p) == 'previously-unbound'
    reg = json.loads(p.read_text())
    reg['skus']['nikon-24mm-f1-8']['identity']['image_catalog'] = 'https://stock.jpg'
    p.write_text(json.dumps(reg))
    assert sr.bind_listing('nikon-24mm-f1-8', _item('210', 't', '20080', cat='https://other.jpg'),
                           {}, path=p) == 'bound'
    e = sr.load_registry(p)['skus']['nikon-24mm-f1-8']
    assert e['identity']['image_catalog'] == 'https://stock.jpg'
    assert e['identity']['image'] == 'https://img/210.jpg'


def test_bound_receipts_survive_a_later_resolve():
    existing = {'bound_listings': [{'item_id': '202'}], 'identity': {}}
    merged = sr._merge_enrichment(existing, {'identity': {}, 'gtin': None})
    assert merged['bound_listings'] == [{'item_id': '202'}]


def test_dry_run_writes_nothing(tmp_path):
    p = _spine(tmp_path)
    before = p.read_text()
    ebay = FakeEbay({Q1: [_cand('202', 'Z 24')]},
                    {'v1|202|0': _item('202', 'Nikon NIKKOR Z 24mm f/1.8 S', '20080')})
    lines = []
    bl.run(slugs=['nikon-24mm-f1-8'], ebay=ebay, path=p, sleep_s=0, out=lines.append)
    assert p.read_text() == before
    assert any('FOUND' in ln and '[202]' in ln for ln in lines)


def test_a_failed_search_is_not_reported_as_no_listing(tmp_path):
    class Down(FakeEbay):
        def search_candidates(self, query, limit=10):
            raise RuntimeError('EBAY_APP_ID not set')
    p = _spine(tmp_path)
    r = bl.find('nikon-24mm-f1-8', sr.load_registry(p)['skus']['nikon-24mm-f1-8'],
                ebay=Down({}, {}), sleep_s=0)
    assert r['verdict'] == 'SEARCH-FAILED'
