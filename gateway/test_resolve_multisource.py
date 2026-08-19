#!/usr/bin/env python3
"""Tests for the GTIN/MPN-first identity ladder (resolve_sku.resolve_multisource
+ the in-place id gate in resolve_proposal). Spec: maddi-multisource-identity-matcher.

Run: python3 -m pytest gateway/test_resolve_multisource.py
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import resolve_sku       # noqa: E402
import skus_registry     # noqa: E402
import review_queue      # noqa: E402
import demand_log        # noqa: E402
# Reuse the shared eBay/Gemma mocks + candidate rows from the sibling suite.
from test_resolve_sku import MockEbay, _gemma, CANDS  # noqa: E402


# ── fixtures (a spine pre-seeded with the proposal slug) ─────────────────────
@pytest.fixture
def skus_path(tmp_path):
    p = tmp_path / 'skus.json'
    p.write_text(json.dumps({
        '_description': 'test', 'version': '0.1.0', 'as_of': '2026-08-19',
        'skus': {
            'sony-a7s-iii': {
                'contamination_key': 'sony-a7s-iii',
                'vendor': 'Sony', 'model': 'A7S III', 'category': 'body',
                'aliases': ['ILCE-7SM3', 'a7siii'],
            },
        },
    }))
    return p


@pytest.fixture
def queue_path(tmp_path):
    return tmp_path / 'review_queue.json'


@pytest.fixture
def demand_path(tmp_path):
    return tmp_path / 'demand_log.jsonl'


def _resolved(*, mpn=None, gtin=None, item_id='v1|100|0'):
    """An ebay.resolve() result carrying a chosen identity's mpn/gtin."""
    ident = {'epid': f'EP-{item_id}', 'legacy_item_id': item_id,
             'ebay_category_id': '625', 'brand': 'Sony',
             'market_title': 'resolved', 'image': 'https://img/x.jpg',
             'price_seen': {'value': '3498', 'currency': 'USD', 'as_of': 'now'}}
    if mpn is not None:
        ident['mpn'] = mpn
    if gtin is not None:
        ident['gtin'] = gtin
    return {'identity': ident, 'affiliate_url': f'https://ebay/itm/{item_id}'}


class MockRung:
    """A source-adapter stand-in: .resolve(target, *rest) -> a preset
    SourceResolution or None."""
    def __init__(self, result):
        self._r = result
        self.calls = []

    def resolve(self, target, *rest):
        self.calls.append((target, rest))
        return self._r


_NULL = MockRung(None)


# ── _id_agreement ────────────────────────────────────────────────────────────
def test_id_agreement_gtin_match_zero_padded():
    assert resolve_sku._id_agreement(
        {'gtin': '0018208027958'}, {'gtin': '18208027958'}) == 'agree'


def test_id_agreement_gtin_mismatch_contradicts():
    assert resolve_sku._id_agreement(
        {'gtin': '0018208027958'}, {'gtin': '9999999999999'}) == 'contradict'


def test_id_agreement_mpn_match_normalized():
    assert resolve_sku._id_agreement(
        {'mpn': 'ILCE-7SM3'}, {'mpn': 'ilce7sm3'}) == 'agree'


def test_id_agreement_mpn_mismatch_contradicts():
    assert resolve_sku._id_agreement({'mpn': '1719'}, {'mpn': '9999'}) == 'contradict'


def test_id_agreement_none_when_no_shared_type():
    assert resolve_sku._id_agreement({'mpn': '1719'}, {'gtin': '123'}) == 'none'
    assert resolve_sku._id_agreement({}, {'mpn': 'x'}) == 'none'


def test_id_agreement_gtin_wins_over_mpn():
    # both carry a GTIN -> GTIN decides even when the MPNs differ
    assert resolve_sku._id_agreement(
        {'gtin': '111', 'mpn': 'A'}, {'gtin': '111', 'mpn': 'B'}) == 'agree'


# ── in-place id gate in resolve_proposal (rules 1 & 2) ───────────────────────
def test_contradiction_routes_to_review_never_mints(skus_path, queue_path, demand_path):
    # The Kodak->nikon-z2 firewall: a confident Gemma pick whose item id
    # CONTRADICTS the carried mpn must NOT reach the spine.
    ebay = MockEbay(candidates=CANDS,
                    resolve_map={'v1|100|0': _resolved(mpn='WRONG-MPN-XZ')})
    out = resolve_sku.resolve_proposal(
        'sony-a7s-iii', ebay=ebay, gemma=_gemma(0, 0.95),  # HIGH confidence
        demand_log=demand_log, review_queue=review_queue,
        mpn='ILCE-7SM3', floor=0.70, skus_path=skus_path,
        review_queue_path=queue_path, demand_log_path=demand_path)
    assert out['outcome'] == 'queued'
    assert out['reason'] == 'identity_contradiction'
    pend = review_queue.load_pending(queue_path)
    assert pend and pend[0]['reason'] == 'identity_contradiction'
    # spine NOT written
    entry = skus_registry.load_registry(skus_path)['skus']['sony-a7s-iii']
    assert not skus_registry.get_marketplace_id(entry, 'ebay_legacy_item_id')


def test_agreement_straightthrough_below_floor(skus_path, queue_path, demand_path):
    # rule 1: an exact id match resolves straight through even when Gemma's
    # confidence is BELOW the floor (strength beats the floor).
    ebay = MockEbay(candidates=CANDS,
                    resolve_map={'v1|100|0': _resolved(mpn='ILCE-7SM3')})
    out = resolve_sku.resolve_proposal(
        'sony-a7s-iii', ebay=ebay, gemma=_gemma(0, 0.40),  # BELOW floor
        demand_log=demand_log, review_queue=review_queue,
        mpn='ILCE-7SM3', floor=0.70, skus_path=skus_path,
        review_queue_path=queue_path, demand_log_path=demand_path)
    assert out['outcome'] == 'resolved'
    assert out['deterministic'] is True
    assert review_queue.load_pending(queue_path) == [] or not queue_path.exists()


def test_no_carried_id_preserves_low_conf_review(skus_path, queue_path, demand_path):
    # 'none' agreement -> unchanged behaviour: low confidence still enqueues.
    ebay = MockEbay(candidates=CANDS)
    out = resolve_sku.resolve_proposal(
        'sony-a7s-iii', ebay=ebay, gemma=_gemma(0, 0.40),  # below floor, no id
        demand_log=demand_log, review_queue=review_queue,
        floor=0.70, skus_path=skus_path,
        review_queue_path=queue_path, demand_log_path=demand_path)
    assert out['outcome'] == 'queued'
    assert review_queue.load_pending(queue_path)[0]['reason'] == 'low_resolve_confidence'


# ── resolve_multisource escalation (rule 3 + off-market propose + floor) ──────
def test_ebay_hit_does_not_escalate(skus_path, queue_path, demand_path):
    ebay = MockEbay(candidates=CANDS,
                    resolve_map={'v1|100|0': _resolved(mpn='ILCE-7SM3')})
    c, d = MockRung(None), MockRung(None)
    out = resolve_sku.resolve_multisource(
        'sony-a7s-iii', ebay=ebay, gemma=_gemma(0, 0.95),
        demand_log=demand_log, review_queue=review_queue,
        mfr_surface=c, xconfirm=d, floor=0.70, skus_path=skus_path,
        review_queue_path=queue_path, demand_log_path=demand_path)
    assert out['outcome'] == 'resolved'
    assert c.calls == [] and d.calls == []   # no escalation when eBay resolves


def test_ebay_miss_confident_source_proposes_offmarket(skus_path, queue_path, demand_path):
    ebay = MockEbay(candidates=[])   # no eBay candidate -> Route 1 miss
    c = MockRung({'source': 'mfr_surface',
                  'identity': {'gtin': '0018208027958', 'mpn': '1719',
                               'brand': 'Nikon', 'canonical_model': 'Z5 II',
                               'image': None},
                  'confidence': 1.0, 'deterministic': True,
                  'aliases': ['Nikon Z5 II'],
                  'relations': {'predecessor': ['Z5'], 'competitor': []},
                  'why': 'Hydrogen barcode'})
    out = resolve_sku.resolve_multisource(
        'sony-a7s-iii', ebay=ebay, gemma=_gemma(-1, 0.0),
        demand_log=demand_log, review_queue=review_queue,
        mfr_surface=c, xconfirm=MockRung(None), floor=0.70, skus_path=skus_path,
        review_queue_path=queue_path, demand_log_path=demand_path)
    assert out['outcome'] == 'queued' and out['reason'] == 'sourced_offmarket'
    rec = review_queue.load_pending(queue_path)[0]
    assert rec['reason'] == 'sourced_offmarket'
    assert rec['contamination']['aliases'] == ['Nikon Z5 II']
    assert rec['contamination']['relations']['predecessor'] == ['Z5']
    # unmet demand NOT logged — the identity was recovered off-market
    assert not demand_path.exists()


def test_ebay_miss_ambiguous_routes_to_review(skus_path, queue_path, demand_path):
    ebay = MockEbay(candidates=[])
    d = MockRung({'source': 'icecat_wikidata', 'identity': None,
                  'confidence': 0.40, 'deterministic': False, 'ambiguous': True,
                  'aliases': ['Lumix L10', 'DMC-L10'],
                  'relations': {'predecessor': [], 'competitor': []},
                  'why': '2 distinct products across eras (2007, 2025)'})
    out = resolve_sku.resolve_multisource(
        'sony-a7s-iii', ebay=ebay, gemma=_gemma(-1, 0.0),
        demand_log=demand_log, review_queue=review_queue,
        mfr_surface=MockRung(None), xconfirm=d, floor=0.70, skus_path=skus_path,
        review_queue_path=queue_path, demand_log_path=demand_path)
    assert out['outcome'] == 'queued' and out['reason'] == 'ambiguous_identity'
    assert review_queue.load_pending(queue_path)[0]['reason'] == 'ambiguous_identity'


def test_all_rungs_miss_logs_unmet_at_floor(skus_path, queue_path, demand_path):
    ebay = MockEbay(candidates=[])
    out = resolve_sku.resolve_multisource(
        'sony-a7s-iii', ebay=ebay, gemma=_gemma(-1, 0.0),
        demand_log=demand_log, review_queue=review_queue,
        mfr_surface=MockRung(None), xconfirm=MockRung(None), floor=0.70,
        skus_path=skus_path, review_queue_path=queue_path,
        demand_log_path=demand_path)
    assert out['outcome'] == 'no_candidate'
    assert out.get('escalated') is True
    # the true "no identity anywhere" floor: demand logged exactly here
    assert demand_path.exists()
    assert review_queue.load_pending(queue_path) == []
