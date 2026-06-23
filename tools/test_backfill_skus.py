"""
Unit tests for backfill_skus — scorer + confidence gate.
========================================================
Pins the decision logic that picks WHICH eBay identity seeds each card. A wrong
pick poisons the seed cadre's join key, so the gate behavior (auto_accept vs
needs_review vs gemma) is what matters most here. Offline: _search_candidates
and resolve are monkeypatched, no creds/network.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import backfill_skus as bf  # noqa: E402
import ebay_api             # noqa: E402


SONY_CARD = {
    'identity': {
        'display_name': 'Sony A7 IV',
        'brand': 'Sony',
        'model': 'A7 IV (ILCE-7M4)',
        'sku_alt_names': ['ILCE-7M4', 'Alpha 7 IV', 'a7 IV'],
    },
    'category': 'body',
}


# ─── scorer ─────────────────────────────────────────────────────────────────

def test_score_exact_product_scores_high():
    s = bf._score(SONY_CARD['identity'],
                  'Sony Alpha A7 IV ILCE-7M4 Mirrorless Camera Body')
    assert s >= bf.ACCEPT_THRESHOLD


def test_score_wrong_product_scores_low():
    # The classic confusable: a DIFFERENT Sony body must not clear the gate.
    s = bf._score(SONY_CARD['identity'],
                  'Sony Alpha A7 III ILCE-7M3 Mirrorless Camera')
    assert s < bf.ACCEPT_THRESHOLD


def test_score_accessory_scores_low():
    s = bf._score(SONY_CARD['identity'], 'Camera Bag for Mirrorless Cameras')
    assert s < bf.ACCEPT_THRESHOLD


# ─── gate: auto_accept ──────────────────────────────────────────────────────

def _patch(monkeypatch, candidates, resolved=None):
    monkeypatch.setattr(ebay_api, '_search_candidates', lambda q, limit=10: candidates)
    if resolved is not None:
        monkeypatch.setattr(ebay_api, 'resolve', lambda item_id, customid=None: resolved)


def _resolved():
    return {
        'identity': {'epid': '15042899333', 'legacy_item_id': '123',
                     'ebay_category_id': '31388', 'brand': 'Sony', 'mpn': 'ILCE-7M4',
                     'market_title': 'Sony A7 IV', 'image': 'x',
                     'price_seen': {'value': '2498', 'currency': 'USD', 'as_of': 'now'}},
        'affiliate_url': 'https://www.ebay.com/itm/1?campid=5339138080',
        '_raw': {},
    }


def test_gate_auto_accepts_strong_match(monkeypatch):
    cands = [
        {'item_id': 'v1|1|0', 'title': 'Sony Alpha A7 IV ILCE-7M4 Body', 'epid': '', 'brand': ''},
        {'item_id': 'v1|2|0', 'title': 'Random unrelated lens cap', 'epid': '', 'brand': ''},
    ]
    _patch(monkeypatch, cands, _resolved())
    entry, review = bf.backfill_card('sony-a7iv', SONY_CARD)
    assert review['decision'] == 'auto_accept'
    assert review['chosen']['item_id'] == 'v1|1|0'
    assert entry is not None
    assert entry['identity']['epid'] == '15042899333'
    assert entry['contamination_key'] == 'sony-a7-iv'


def test_gate_needs_review_when_no_strong_match(monkeypatch):
    # Only weak/wrong candidates -> no auto-accept, no entry produced.
    cands = [
        {'item_id': 'v1|9|0', 'title': 'Sony A7 III ILCE-7M3 Body', 'epid': '', 'brand': ''},
        {'item_id': 'v1|8|0', 'title': 'Camera strap universal', 'epid': '', 'brand': ''},
    ]
    _patch(monkeypatch, cands)
    entry, review = bf.backfill_card('sony-a7iv', SONY_CARD)
    assert review['decision'] == 'needs_review'
    assert entry is None  # nothing seeded on a weak match — the whole point


def test_gate_no_candidates(monkeypatch):
    _patch(monkeypatch, [])
    entry, review = bf.backfill_card('sony-a7iv', SONY_CARD)
    assert review['decision'] == 'no_candidates'
    assert entry is None


def test_rejected_alternatives_recorded(monkeypatch):
    cands = [
        {'item_id': 'v1|1|0', 'title': 'Sony Alpha A7 IV ILCE-7M4 Body', 'epid': '', 'brand': ''},
        {'item_id': 'v1|2|0', 'title': 'Sony A7 III ILCE-7M3', 'epid': '', 'brand': ''},
    ]
    _patch(monkeypatch, cands, _resolved())
    _, review = bf.backfill_card('sony-a7iv', SONY_CARD)
    assert len(review['rejected']) >= 1
    assert review['rejected'][0]['title'].startswith('Sony A7 III')


# ─── gemma escalation ───────────────────────────────────────────────────────

def test_gemma_escalation_accepts_when_shim_picks(monkeypatch):
    cands = [
        {'item_id': 'v1|7|0', 'title': 'Sony ILCE 7M4 (ambiguous listing)', 'epid': '', 'brand': ''},
        {'item_id': 'v1|6|0', 'title': 'Sony body unknown', 'epid': '', 'brand': ''},
    ]
    _patch(monkeypatch, cands, _resolved())
    # Force weak scores so we fall to the gemma tier, then have gemma pick v1|7|0.
    monkeypatch.setattr(bf, '_score', lambda idn, title: 0.1)
    monkeypatch.setattr(bf, '_gemma_adjudicate', lambda idn, c: 'v1|7|0')
    entry, review = bf.backfill_card('sony-a7iv', SONY_CARD, use_gemma=True)
    assert review['decision'] == 'gemma_accept'
    assert review['gemma_used'] is True
    assert entry is not None


def test_gemma_no_match_falls_to_review(monkeypatch):
    cands = [{'item_id': 'v1|7|0', 'title': 'something', 'epid': '', 'brand': ''}]
    _patch(monkeypatch, cands)
    monkeypatch.setattr(bf, '_score', lambda idn, title: 0.1)
    monkeypatch.setattr(bf, '_gemma_adjudicate', lambda idn, c: None)
    entry, review = bf.backfill_card('sony-a7iv', SONY_CARD, use_gemma=True)
    assert review['decision'] == 'gemma_no_match'
    assert entry is None


def test_category_backfill_flagged(monkeypatch):
    lens_card = {
        'identity': {'display_name': 'Sigma 35mm f/1.4 DG DN Art II',
                     'brand': 'Sigma', 'model': '35mm f/1.4 DG DN Art II',
                     'sku_alt_names': ['340969']},
        'category': None,
    }
    cands = [{'item_id': 'v1|5|0',
              'title': 'Sigma 35mm f/1.4 DG DN Art II Lens 340969', 'epid': '', 'brand': ''}]
    _patch(monkeypatch, cands, _resolved())
    entry, review = bf.backfill_card('sigma-35-art-dg-dn-ii', lens_card)
    assert review.get('category_backfilled') == 'lens'
    assert entry['category'] == 'lens'
