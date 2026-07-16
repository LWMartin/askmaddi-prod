"""
Tests for rebind_firewall — the sanity gate on resolve-path identity rebinds.
=============================================================================
Pins the 2026-07-16 live incident (empty-box rebind on ulanzi-f38-zero) and
every rule of the gate: junk-title hard reject, two-soft-signal reject,
single-soft pass, thin-prior pass, receipt idempotency, and the route-3
posture (standing identity untouched, outcome loud, receipt parked).
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rebind_firewall as fw       # noqa: E402
import skus_registry as reg        # noqa: E402
import resolve_sku                 # noqa: E402
import review_queue                # noqa: E402

# The LIVE poison title, verbatim from the 2026-07-16 spine diff.
POISON_TITLE = ('\u201cEmpty Box\u201d For the Ulanzi Coman Zero F38 '
                'Carbon Fiber Camera Tripod')


def _entry(title='Ulanzi Zero F38 Quick Release Travel Tripod',
           price='404.59', mpn='ULZ-3131', cat='30097', legacy='365318479476'):
    return {
        'identity': {'market_title': title, 'mpn': mpn,
                     'price_seen': {'value': price, 'currency': 'USD'}},
        'marketplace_ids': {'ebay_legacy_item_id': legacy},
        'marketplace_categories': {'ebay_category_id': cat},
    }


# ── is_junk_title ────────────────────────────────────────────────────────────

def test_live_poison_title_is_junk():
    assert fw.is_junk_title(POISON_TITLE) is True


def test_word_boundary_no_false_positive():
    # 'cap' must not fire inside 'Capture'; 'skin' not inside 'Skinner'.
    assert fw.is_junk_title('Peak Design Capture Clip Tripod Capture') is False
    assert fw.is_junk_title('Skinner Optics Field Tripod') is False


def test_accessory_word_fires_on_boundary():
    assert fw.is_junk_title('Body Cap for Sony E-Mount') is True
    assert fw.is_junk_title('Sony A7S III for parts, read description') is True


# ── assess ───────────────────────────────────────────────────────────────────

def test_live_incident_hard_rejects():
    old = _entry()
    new = _entry(title=POISON_TITLE, price='25.00', mpn='', cat='30093',
                 legacy='257392871924')
    out = fw.assess(old, new)
    assert out['verdict'] == 'reject'
    assert out['hard'] is True
    assert 'junk_title' in out['signals']
    # the soft signals were all present too
    assert {'price_collapse', 'mpn_wiped', 'category_flip'} <= set(out['signals'])


def test_two_soft_signals_reject_without_junk_title():
    old = _entry()
    new = _entry(title='Ulanzi Coman Zero F38 Tripod', price='25.00',
                 mpn='', legacy='999')
    out = fw.assess(old, new)
    assert out['verdict'] == 'reject'
    assert out['hard'] is False
    assert set(out['signals']) == {'price_collapse', 'mpn_wiped'}


def test_single_soft_signal_passes():
    # A legitimate listing rotation with a real price drop is market noise.
    old = _entry()
    new = _entry(price='99.00', legacy='999')   # collapse alone
    assert fw.assess(old, new)['verdict'] == 'pass'
    new2 = _entry(mpn='', legacy='999')          # wipe alone
    assert fw.assess(old, new2)['verdict'] == 'pass'


def test_thin_prior_never_manufactures_suspicion():
    # Standing entry has no price/mpn/category -> nothing to compare against.
    old = {'identity': {'market_title': 'x'}, 'marketplace_ids': {}}
    new = _entry(price='25.00', mpn='', legacy='999')
    assert fw.assess(old, new)['verdict'] == 'pass'


def test_clean_rotation_passes():
    old = _entry()
    new = _entry(price='389.00', legacy='999')  # same product, new listing
    out = fw.assess(old, new)
    assert out['verdict'] == 'pass'
    assert out['signals'] == []


# ── set_rebind_rejection ─────────────────────────────────────────────────────

def _seed(tmp_path):
    p = tmp_path / 'skus.json'
    p.write_text(json.dumps({'_description': 't', 'version': '0.1.0',
                             'as_of': 'x', 'skus': {'ulanzi-f38-zero': _entry()}}))
    return p


def test_rejection_receipt_written_and_idempotent(tmp_path):
    p = _seed(tmp_path)
    receipt = {'ts': 't1', 'item_id': '257392871924', 'title': POISON_TITLE,
               'signals': ['junk_title'], 'hard': True}
    assert reg.set_rebind_rejection('ulanzi-f38-zero', receipt, path=p) == 'written'
    # daily re-offer of the same junk listing: no churn
    receipt2 = dict(receipt, ts='t2')
    assert reg.set_rebind_rejection('ulanzi-f38-zero', receipt2, path=p) == 'unchanged'
    e = json.loads(p.read_text())['skus']['ulanzi-f38-zero']
    assert e['rebind_rejected']['ts'] == 't1'
    # standing identity untouched
    assert e['identity']['mpn'] == 'ULZ-3131'


def test_rejection_receipt_replaces_on_new_listing(tmp_path):
    p = _seed(tmp_path)
    reg.set_rebind_rejection('ulanzi-f38-zero',
                             {'item_id': 'a', 'signals': ['junk_title']}, path=p)
    assert reg.set_rebind_rejection(
        'ulanzi-f38-zero', {'item_id': 'b', 'signals': ['junk_title']},
        path=p) == 'written'


def test_rejection_missing_slug(tmp_path):
    p = _seed(tmp_path)
    assert reg.set_rebind_rejection('nope', {'item_id': 'a'}, path=p) == 'missing-slug'


def test_lawful_rebind_clears_receipt_by_construction(tmp_path):
    """upsert's wholesale replace drops rebind_rejected — the receipt dies
    with the identity it warned about."""
    p = _seed(tmp_path)
    reg.set_rebind_rejection('ulanzi-f38-zero',
                             {'item_id': 'x', 'signals': ['junk_title']}, path=p)
    fresh = reg.build_entry(
        slug='ulanzi-f38-zero', vendor='Ulanzi', model='Zero F38',
        facet='tripod', contamination_key='ulanzi-f38',
        resolved={'identity': {'epid': 'E9', 'legacy_item_id': '111',
                               'market_title': 'Ulanzi Zero F38 Tripod',
                               'mpn': 'ULZ-3131',
                               'price_seen': {'value': '389.00'}},
                  'affiliate_url': 'https://x'})
    reg.upsert('ulanzi-f38-zero', fresh, path=p)
    e = json.loads(p.read_text())['skus']['ulanzi-f38-zero']
    assert 'rebind_rejected' not in e


# ── route-3 integration ──────────────────────────────────────────────────────

class _Ebay:
    class EbayAPIError(Exception):
        pass

    def __init__(self, resolved_identity):
        self._identity = resolved_identity

    def is_configured(self):
        return True

    def _search_candidates(self, query, limit=10):
        return [{'item_id': 'v1|257392871924|0', 'title': self._identity['market_title'],
                 'price': self._identity['price_seen']['value'], 'currency': 'USD',
                 'condition': 'New', 'epid': '', 'brand': 'Ulanzi'}]

    def resolve(self, item_id, customid=None):
        return {'identity': dict(self._identity), 'affiliate_url': 'https://ebay/itm/poison'}


def _gemma_confident():
    payload = json.dumps({'index': 0, 'confidence': 0.95, 'why': 'mock'})
    return resolve_sku.GemmaDisambiguator(client=lambda prompt: payload)


def test_route3_rebind_rejected_parks_receipt_keeps_spine(tmp_path):
    skus_path = tmp_path / 'skus.json'
    standing = _entry()
    standing.update({'contamination_key': 'ulanzi-f38', 'vendor': 'Ulanzi',
                     'model': 'Zero F38', 'category': 'tripod',
                     'aliases': ['F38']})
    skus_path.write_text(json.dumps({'_description': 't', 'version': '0.1.0',
                                     'as_of': 'x',
                                     'skus': {'ulanzi-f38-zero': standing}}))
    poison = {'epid': '', 'legacy_item_id': '257392871924',
              'ebay_category_id': '30093', 'brand': 'Ulanzi', 'mpn': '',
              'market_title': POISON_TITLE,
              'price_seen': {'value': '25.00', 'currency': 'USD', 'as_of': 'now'}}
    import demand_log
    out = resolve_sku.resolve_proposal(
        'ulanzi-f38-zero', ebay=_Ebay(poison), gemma=_gemma_confident(),
        demand_log=demand_log, review_queue=review_queue,
        floor=0.70, skus_path=skus_path,
        review_queue_path=tmp_path / 'rq.json',
        demand_log_path=tmp_path / 'dl.jsonl')
    assert out['outcome'] == 'rebind_rejected'
    assert 'junk_title' in out['detail']
    e = json.loads(skus_path.read_text())['skus']['ulanzi-f38-zero']
    # spine identity untouched by the poison
    assert e['identity']['mpn'] == 'ULZ-3131'
    assert e['identity']['market_title'].startswith('Ulanzi Zero F38')
    # receipt parked for /admin
    assert e['rebind_rejected']['item_id'] == '257392871924'
    assert e['rebind_rejected']['hard'] is True
