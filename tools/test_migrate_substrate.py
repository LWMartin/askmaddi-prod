"""
migrate_substrate tests — the settled-data safety net.

The migration runs ONCE over the live 14-entry spine on the box; these tests
are what make that run boring. Fixtures model the real shape families the
live spine carries: pre-L1 (no gtin key), L1-set, L1-null with a conflict
receipt, and a receipt carrying adjudication events (the append-only human
ruling that must survive the hoist byte-identical).
"""

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'gateway'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import skus_registry              # noqa: E402
import migrate_substrate as ms    # noqa: E402


RECEIPT = {
    'chosen_source': None, 'conflict': True,
    'observations': [{'source': 'product.gtins', 'raw': 'x',
                      'gtin14': '00000000000017', 'valid': True}],
    'recovery': {'method': 'ebay-secondpass', 'verdict': 'CONFLICT_DROP',
                 'query': 'q', 'recovered_at': 'T', 'model_token': 'M',
                 'n_candidates': 2, 'n_gtin_bearing': 2,
                 'distinct_gtins': ['00000000000017', '00000000000024'],
                 'candidates': []},
}

RULED_RECEIPT = dict(copy.deepcopy(RECEIPT),
                     adjudications=[{'action': 'dismiss', 'actor': 'admin',
                                     'at': 'T', 'reason': 'variant_ambiguous'}])


def _old_registry(tmp_path):
    """The live spine's shape families, old-shape."""
    path = tmp_path / 'skus.json'
    path.write_text(json.dumps({
        '_description': 'test', 'version': '0.1.0', 'as_of': '2026-07-01',
        'skus': {
            'pre-l1': {                       # oldest family: no gtin key at all
                'vendor': 'Ulanzi', 'model': 'MT-79', 'category': 'support',
                'contamination_key': 'ulanzi-mt-79',
                'identity': {'brand': 'Ulanzi', 'mpn': 'MT-79',
                             'epid': '', 'legacy_item_id': 'v1|555|0',
                             'ebay_category_id': '30093'},
                'affiliate': {'ebay_epn_url': 'u', 'amazon_asin': None},
            },
            'l1-set': {                       # anchor already earned at L1
                'vendor': 'Canon', 'model': 'R5', 'category': 'body',
                'contamination_key': 'canon-r5',
                'identity': {'brand': 'Canon', 'mpn': 'R5',
                             'gtin': '00013803323114',
                             'epid': 'EP9', 'legacy_item_id': 'v1|900|0',
                             'ebay_category_id': '31388',
                             'gtin_provenance': {'chosen_source': 'product.gtins',
                                                 'conflict': False,
                                                 'observations': []}},
                'affiliate': {'ebay_epn_url': 'u', 'amazon_asin': None},
            },
            'conflict': {                     # null anchor + live receipt
                'vendor': 'Peak Design', 'model': 'Travel Tripod',
                'category': 'support', 'contamination_key': 'pd-tt',
                'identity': {'brand': 'Peak Design', 'mpn': 'TT',
                             'gtin': None, 'epid': '',
                             'legacy_item_id': 'v1|111|0',
                             'ebay_category_id': '30093',
                             'gtin_provenance': copy.deepcopy(RECEIPT)},
                'affiliate': {'ebay_epn_url': 'u', 'amazon_asin': None},
            },
            'ruled': {                        # dismissed by human — MUST survive
                'vendor': 'Peak Design', 'model': 'Pro Tripod',
                'category': 'support', 'contamination_key': 'pd-pt',
                'identity': {'brand': 'Peak Design', 'mpn': 'PT',
                             'gtin': None, 'epid': '',
                             'legacy_item_id': 'v1|222|0',
                             'ebay_category_id': '30093',
                             'gtin_provenance': copy.deepcopy(RULED_RECEIPT)},
                'affiliate': {'ebay_epn_url': 'u', 'amazon_asin': None},
            },
        }}))
    return str(path)


def test_hoist_shapes_all_families(tmp_path):
    path = _old_registry(tmp_path)
    assert ms.run(skus_path=path, commit=True, out=lambda *a: None) == 0
    skus = json.load(open(path))['skus']

    # frozen slugs untouched
    assert sorted(skus) == ['conflict', 'l1-set', 'pre-l1', 'ruled']

    for slug, e in skus.items():
        # Axis A/B homes exist on every entry
        assert 'gtin' in e and 'marketplace_ids' in e
        assert 'facet' in e and 'marketplace_categories' in e
        assert e['unspsc'] is None
        # hoisted keys gone from identity, category gone from top level
        assert 'category' not in e
        for k in ('gtin', 'epid', 'legacy_item_id', 'ebay_category_id'):
            assert k not in e['identity'], (slug, k)

    assert skus['pre-l1']['gtin'] is None
    assert skus['pre-l1']['marketplace_ids']['ebay_legacy_item_id'] == 'v1|555|0'
    assert skus['pre-l1']['facet'] == 'support'
    assert skus['pre-l1']['marketplace_categories']['ebay_category_id'] == '30093'

    assert skus['l1-set']['gtin'] == '00013803323114'
    assert skus['l1-set']['needs_review'] is True     # unspsc null forces it
    assert skus['conflict']['needs_review'] is True   # gtin null too


def test_receipts_and_rulings_survive_byte_identical(tmp_path):
    path = _old_registry(tmp_path)
    ms.run(skus_path=path, commit=True, out=lambda *a: None)
    skus = json.load(open(path))['skus']
    # evidence stays in identity, untouched — including the human ruling
    assert skus['conflict']['identity']['gtin_provenance'] == RECEIPT
    assert skus['ruled']['identity']['gtin_provenance'] == RULED_RECEIPT


def test_idempotent_second_run_is_noop(tmp_path):
    path = _old_registry(tmp_path)
    ms.run(skus_path=path, commit=True, out=lambda *a: None)
    first = open(path).read()
    ms.run(skus_path=path, commit=True, out=lambda *a: None)
    second = json.load(open(path))
    # entries byte-identical; only difference permitted is nothing at all —
    # a no-op run appends no receipt and rewrites nothing
    assert json.load(open(path))['skus'] == json.loads(first)['skus']
    assert len(second['substrate']) == 1


def test_mixed_registry_migrates_only_old_shape(tmp_path):
    path = _old_registry(tmp_path)
    reg = json.load(open(path))
    # a new-shape mint landed before migration ran (legal mixed state)
    reg['skus']['fresh-mint'] = skus_registry.build_entry(
        'fresh-mint', 'Sony', 'A1 II', 'body', 'sony-a1-ii',
        {'identity': {'brand': 'Sony', 'mpn': 'ILCE-1M2',
                      'gtin': '00027242927834', 'epid': 'EP1',
                      'legacy_item_id': 'v1|1|0', 'ebay_category_id': '31388'},
         'affiliate_url': 'u'})
    open(path, 'w').write(json.dumps(reg))
    before = json.dumps(json.load(open(path))['skus']['fresh-mint'],
                        sort_keys=True)
    ms.run(skus_path=path, commit=True, out=lambda *a: None)
    after = json.load(open(path))['skus']['fresh-mint']
    assert json.dumps(after, sort_keys=True) == before   # skipped verbatim


def test_dry_run_default_writes_nothing(tmp_path):
    path = _old_registry(tmp_path)
    before = open(path).read()
    assert ms.run(skus_path=path, out=lambda *a: None) == 0   # no commit flag
    assert open(path).read() == before


def test_commit_appends_substrate_receipt(tmp_path):
    path = _old_registry(tmp_path)
    ms.run(skus_path=path, commit=True, out=lambda *a: None)
    reg = json.load(open(path))
    rec = reg['substrate'][0]
    assert rec['version'] == ms.SUBSTRATE_VERSION
    assert rec['migrated'] == 4 and rec['skipped'] == 0
    assert rec['migrated_at']


# ── accessors: the dual-shape coherence device ──────────────────────────────

OLD = {'category': 'body',
       'identity': {'gtin': 'G1', 'epid': 'E1', 'legacy_item_id': 'L1',
                    'ebay_category_id': 'C1'},
       'affiliate': {'amazon_asin': 'A1'}}
NEW = {'gtin': 'G2', 'facet': 'lens',
       'marketplace_ids': {'ebay_epid': 'E2', 'ebay_legacy_item_id': 'L2',
                           'amazon_asin': 'A2'},
       'marketplace_categories': {'ebay_category_id': 'C2'},
       'identity': {}}


def test_accessors_read_both_shapes():
    assert skus_registry.get_gtin(OLD) == 'G1'
    assert skus_registry.get_gtin(NEW) == 'G2'
    assert skus_registry.get_facet(OLD) == 'body'
    assert skus_registry.get_facet(NEW) == 'lens'
    assert skus_registry.get_marketplace_id(OLD, 'ebay_epid') == 'E1'
    assert skus_registry.get_marketplace_id(NEW, 'ebay_epid') == 'E2'
    assert skus_registry.get_marketplace_id(OLD, 'ebay_legacy_item_id') == 'L1'
    assert skus_registry.get_marketplace_id(NEW, 'ebay_legacy_item_id') == 'L2'
    assert skus_registry.get_marketplace_id(OLD, 'amazon_asin') == 'A1'
    assert skus_registry.get_marketplace_id(NEW, 'amazon_asin') == 'A2'
    assert skus_registry.get_marketplace_category(OLD) == 'C1'
    assert skus_registry.get_marketplace_category(NEW) == 'C2'


def test_accessor_new_shape_null_does_not_fall_back():
    # A migrated entry with a null anchor is NULL — the accessor must not
    # resurrect a stale old-shape shadow (there is none post-migration, but
    # the presence check is on the KEY, not truthiness, by design).
    e = {'gtin': None, 'identity': {'gtin': 'STALE'}}
    assert skus_registry.get_gtin(e) is None
