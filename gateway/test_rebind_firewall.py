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


def test_accessory_word_alone_no_longer_hard_fires():
    """2026-07-18: bundle language is not junk. The firewall's first live
    morning hard-rejected two legitimate body listings on bare 'charger';
    accessory nouns are now soft evidence (paired with price_collapse)."""
    assert fw.is_junk_title(
        'Sony Alpha a7C 24.2MP Mirrorless Camera W/Batts & Charger'
        ' - Silver (Body Only)') is False
    assert fw.is_junk_title('Body Cap for Sony E-Mount') is True   # phrase
    assert fw.is_junk_title('Sony A7S III for parts, read description') is True
    assert fw.has_accessory_vocab('Camera W/Batts & Charger') is True
    assert fw.has_accessory_vocab('Sony A7C Mirrorless Camera Body') is False


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


# ── 2026-07-18 rework: relational rule + accessory pairing ───────────────────
# Pins for the firewall's first live morning (two false positives) and the
# competitor-listing class the dry-run surfaced.

def _standing_a7c():
    e = _entry(title='Sony Alpha a7C 24.2MP Mirrorless Camera - Silver'
                     ' (Body Only)',
               price='995.00', mpn='ILCE7C/S', cat='31388', legacy='111')
    e['identity']['brand'] = 'Sony'
    e['model'] = 'A7C'
    return e


def test_live_false_positive_a7c_bundle_now_passes():
    """The 2026-07-18 04:09 rejection, replayed: bundle title, no MPN,
    full price. accessory_vocab + mpn_wiped must NOT reject — accessory
    vocabulary only pairs with price_collapse."""
    old = _standing_a7c()
    new = _entry(title='Sony Alpha a7C 24.2MP Mirrorless Camera W/Batts &'
                       ' Charger - Silver (Body Only)',
                 price='1000.00', mpn='', cat='31388', legacy='227431245419')
    new['identity']['brand'] = 'Sony'
    out = fw.assess(old, new)
    assert out['verdict'] == 'pass'
    assert 'junk_title' not in out['signals']
    assert set(out['signals']) == {'mpn_wiped', 'accessory_vocab'}


def test_live_false_positive_a7r_bundle_now_passes():
    """The 2026-07-18 04:13 rejection, replayed."""
    old = _entry(title='Sony Alpha a7R III ILCE-7RM3 42.4MP Mirrorless'
                       ' Camera Body', price='1100.00', mpn='ILCE7RM2',
                 cat='31388', legacy='222')
    old['identity']['brand'] = 'Sony'
    old['model'] = 'A7R II'
    new = _entry(title='Sony Alpha ILCE-7RM2 A7R ii A7R II 42.4MP Digital'
                       ' Camera(Only Body) with Charger',
                 price='609.00', mpn='', cat='31388', legacy='398111491854')
    out = fw.assess(old, new)
    assert out['verdict'] == 'pass'


def test_accessory_only_listing_rejects_via_price_pairing():
    """The class the old bare-word gate was aiming at: a genuine
    accessory listing naming the model. Cheap + accessory vocab = reject,
    without any fatal word."""
    old = _standing_a7c()
    new = _entry(title='Sony A7C Battery Charger BC-QZ1 Genuine OEM',
                 price='49.00', mpn='', cat='48519', legacy='333')
    out = fw.assess(old, new)
    assert out['verdict'] == 'reject'
    assert out['hard'] is False
    assert {'accessory_vocab', 'price_collapse'} <= set(out['signals'])


def test_relational_rule_catches_competitor_listing():
    """Registry-history specimen: NEEWER tripod 'for Peak Design' filed
    under peak-design-travel-tripod. No junk vocabulary, plausible price —
    only the relational construction gives it away."""
    old = _entry(title='Peak Design Travel Tripod Carbon Fiber Tripod Ball'
                       ' Head Quick Release', price='375.00', mpn='',
                 cat='30093', legacy='444')
    old['identity']['brand'] = 'Peak Design'
    old['model'] = 'Travel Tripod'
    new = _entry(title="NEEWER LITETRIP LT32 62''Travel Tripod Carbon Fiber"
                       ' for Peak Design Camera', price='349.00', mpn='X1',
                 cat='30093', legacy='555')
    out = fw.assess(old, new)
    assert out['verdict'] == 'reject'
    assert out['hard'] is True
    assert 'relational_for' in out['signals']


def test_relational_rule_ignores_generic_for_phrases():
    """'for Camera', 'for Travel and Hiking', 'for L mount' are uses, not
    the standing identity. Model anchors match as whole phrases only, so
    model 'Travel Tripod' never fires on the bare word 'travel'."""
    anchors = ['peak design', 'travel tripod']
    assert fw.is_relational_reject(
        'Peak Design Carbon Fiber Travel Tripod for Camera and Phone',
        anchors) is False
    assert fw.is_relational_reject(
        'Carbon Fiber Tripod for Travel and Hiking', anchors) is False
    assert fw.is_relational_reject(
        'Sigma 35mm F/1.2 DG DN Art (for L mount) #287',
        ['sigma', '35mm f/1.2 dg dn']) is False
    assert fw.is_relational_reject(
        "NEEWER Travel Tripod for Peak Design Camera", anchors) is True


def test_relational_original_poison_second_path():
    """The 2026-07-16 poison title is ALSO a 'For the <brand>'
    construction — the relational rule catches the original incident
    independently of the 'empty box' phrase."""
    assert fw.is_relational_reject(POISON_TITLE, ['ulanzi', 'f38']) is True


def test_anchor_extraction_filters_generic_variants():
    """'F38 / Zero' yields 'f38' (digit) but not bare 'zero'; multiword
    models survive; brand comes from identity.brand or vendor."""
    e = {'identity': {'brand': 'Ulanzi'}, 'model': 'F38 / Zero',
         'aliases': ['F38']}
    anchors = fw._anchor_phrases(e)
    assert 'ulanzi' in anchors and 'f38' in anchors
    assert 'zero' not in anchors
    e2 = {'vendor': 'Peak Design', 'model': 'Travel Tripod'}
    assert fw._anchor_phrases(e2) == ['peak design', 'travel tripod']


def test_thin_prior_relational_never_fires():
    """No brand/model on the standing entry -> no anchors -> the
    relational rule cannot manufacture suspicion."""
    old = {'identity': {'market_title': 'x'}, 'marketplace_ids': {}}
    new = _entry(title='Charger for Sony A7C', price='49.00', legacy='999')
    out = fw.assess(old, new)
    assert 'relational_for' not in out['signals']


# ── registry-history corpus (2026-07-18 dry-run, 36 titles) ──────────────────
# Every listing title the registry had ever seen at rework time, plus the
# purged poison. Hard-gate expectations only (junk/fatal/relational);
# soft signals need entry context and are pinned above. Future verticals:
# append your titles here — this fixture is the false-positive regression
# asset.

_CAMERA = ('sony', None)
CORPUS = [
    # (anchors, title, expect_hard_reject)
    (['peak design', 'travel tripod'], 'Peak Design Pro Tripod', False),
    (['peak design', 'travel tripod'],
     'New Peak Design Pro Carbon Fiber Tripod with Ball Head Black'
     ' PT-S-BK-1', False),
    (['peak design', 'travel tripod'],
     'Peak Design Travel Tripod Carbon Fiber Tripod Ball Head Quick'
     ' Release', False),
    (['peak design', 'travel tripod'],
     'NEW in box Peak Design Aluminum Travel Tripod TT-CB-5-150-AL-1'
     ' W tag', False),
    (['peak design', 'travel tripod'],
     'Peak Design Carbon Fiber Travel Tripod for Camera and Phone'
     ' TT-CB-5-15', False),
    (['peak design', 'travel tripod'],
     'Peak Design Carbon Fiber Travel Tripod for Camera. Original Owner'
     ' - Pr', False),
    (['peak design', 'travel tripod'],
     "NEEWER LITETRIP LT32 62''Travel Tripod Carbon Fiber for Peak Design"
     ' Ca', True),
    (['sigma'], 'Sigma 35mm F1.4 DG II Art Full Frame Wide Angle Prime Lens'
     ' (Sony E-Mount)', False),
    (['sony', 'a7 iv'], 'Sony Alpha A7 IV 33MP Full Frame Interchangeable'
     ' Lens Camera 8,518 Shutter Count', False),
    (['sony', 'a7 iv'], 'Sony a7 IV Mirrorless Alpha Interchangeable Lens'
     ' Camera Body ILCE7M4/B', False),
    (['sony', 'a7 iv'], 'Sony Alpha a7 IV 33MP Mirrorless Camera Kit Lens'
     ' 28-70mm Bag Less Than', False),
    (['sony', 'a7 iv'], '[ Almost Mint  s/c 20814 English language ] Sony'
     ' Alpha a7 IV ILCE-7M4', False),
    (['sony', 'a7 iv'], 'Sony A7 IV (ILCE-7M4) 33 MP Mirrorless Camera in'
     ' Black *Body Only*', False),
    (['sony', 'a7 iv'], 'Sony A7IV ILCE-7M4 A7M4 LCD Screen Monitor Repair'
     ' Replacement Part GENUINE SONY', True),
    (['sony', 'a7s iii'], 'Sony a7S III Mirrorless Camera Body ILCE-7SM3 —'
     ' 3,659 Shutter Count + 256GB V60', False),
    (['sony', 'a7v'], 'Sony A7V (ILCE-7M5/B) Mirrorless Camera  - US model'
     ' / Brand New', False),
    (['sony', 'a1'], 'Sony A1 [ILCE-1] Body w/ 116,635 Shutter Count – MUST'
     ' READ! (2151)', False),
    (['sony', 'a1'], '424 cuts!! SONY a1 ILCE-1 Mirrorless Digital Camera'
     ' [English OK] 10792', False),
    (['sony', 'a7c'], 'Sony Alpha a7C 24.2MP Mirrorless Camera - Silver'
     ' (Body Only)', False),
    (['sony', 'a7c'], 'Sony Alpha 7C a7C 24.2MP Full-Frame Mirrorless'
     ' Camera - Silver (ILCE7C/S)', False),
    (['sony', 'a7c'], 'Sony Alpha a7C 24.2MP Mirrorless Camera W/Batts &'
     ' Charger - Silver (Body Only)', False),
    (['canon', 'r6'], 'Canon EOS R6 Mirrorless Digital Camera Body, Used,'
     ' Excellent Condition', False),
    (['canon', 'r6'], 'Canon EOS R6 20.1MP Mirrorless Camera (Body Only)',
     False),
    (['manfrotto'], 'Manfrotto Befree 3-Way Live Advanced Tripod - Black'
     ' (MKBFRLA4BK-3WUS)', False),
    (['sigma'], 'Sigma 35mm F/1.2 DG DN Art (for L mount) #287', False),
    (['canon', 'r5'], 'Canon EOS R5 45.0MP Mirrorless Camera - Black 24%'
     ' Shutter Count (Body Only)', False),
    (['sony', 'a7r ii'], 'Sony Alpha a7R III ILCE-7RM3 42.4MP Mirrorless'
     ' Camera Body', False),
    (['sony', 'a7r ii'], 'Sony Alpha A7R II A7RII ILCE7RM2 42.4MP ** UGLY'
     ' but Works * READ NOTES', False),
    (['sony', 'a7r ii'], 'Mint- Sony Alpha A7R II ILCE-7RM2 42.4MP Body'
     ' Only | 5k Shutter', False),
    (['sony', 'a7r ii'], 'Sony Alpha ILCE-7RM2 A7R ii A7R II 42.4MP Digital'
     ' Camera(Only Body) with Charger', False),
    (['ulanzi', 'f38'], 'Ulanzi Zero F38 Carbon Fiber Lightweight Quick'
     ' Release Travel Tripod', False),
    (['ulanzi', 'f38'], POISON_TITLE, True),
]


@pytest.mark.parametrize('anchors,title,expect', CORPUS,
                         ids=[t[:45] for _, t, _ in CORPUS])
def test_corpus_hard_gate(anchors, title, expect):
    got = fw.is_junk_title(title) or fw.is_relational_reject(title, anchors)
    assert got is expect, f'hard-gate flip on: {title!r}'
