"""Tests for resolve_sku.py + the review_queue/admin extensions it depends on.

All offline — eBay and Gemma are INJECTED mocks, never a live network/ollama call.
Every store write goes to a tmp_path, never the real (gitignored) data/ files.

What's proven here (the design decisions, made executable):
  RESOLVER ROUTING
    - confident pick      -> spine write via the EXISTING skus_registry chain
    - low-confidence pick -> review_queue w/ reason low_resolve_confidence + candidates
    - no usable candidate -> demand_log.log_unmet (the unmet-demand signal)
    - eBay API failure propagates (a network blip is NOT "no demand")
    - unknown proposal slug -> ResolveError (factory enriches, never mints)
  DISAMBIGUATOR
    - injectable client (offline); defensive parse; -1 index -> no item_id
  QUEUE EXTENSION
    - enqueue accepts reason_override + candidates; rejects a bad reason
    - existing slug-gate enqueue path is unchanged (no candidates, derived reason)
  RE-RESOLVE CORRECTION LOOP (within limits, no abuse)
    - only a low_resolve_confidence record can be re-resolved
    - chosen item_id MUST be among the record's own candidates
    - identity comes from a real ebay.resolve(), re-froze, record stays pending
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resolve_sku       # noqa: E402
import review_queue      # noqa: E402
import skus_registry     # noqa: E402
import admin_surface     # noqa: E402


# ── mocks ───────────────────────────────────────────────────────────────────

class MockEbay:
    """Stand-in for the ebay_api module: ._search_candidates + .resolve, plus the
    EbayAPIError type and is_configured so admin's reresolve route is testable."""
    class EbayAPIError(Exception):
        pass

    def __init__(self, candidates=None, resolve_map=None, search_raises=False):
        self._candidates = candidates or []
        self._resolve_map = resolve_map or {}
        self._search_raises = search_raises
        self.search_calls = []
        self.resolve_calls = []

    def is_configured(self):
        return True

    def _search_candidates(self, query, limit=10):
        self.search_calls.append((query, limit))
        if self._search_raises:
            raise self.EbayAPIError('browse search failed: HTTP 503')
        return list(self._candidates)

    def resolve(self, item_id, customid=None):
        self.resolve_calls.append(item_id)
        if item_id in self._resolve_map:
            return self._resolve_map[item_id]
        return {
            'identity': {
                'epid': f'EP-{item_id}', 'legacy_item_id': item_id,
                'ebay_category_id': '625', 'brand': 'Sony', 'mpn': 'ILCE-7SM3',
                'market_title': f'resolved {item_id}', 'image': 'https://img/x.jpg',
                'price_seen': {'value': '3498', 'currency': 'USD', 'as_of': 'now'},
            },
            'affiliate_url': f'https://ebay/itm/{item_id}?campid=5339138080',
        }


def _gemma(index, confidence, why='mock'):
    """A GemmaDisambiguator with an injected client returning a fixed verdict."""
    payload = json.dumps({'index': index, 'confidence': confidence, 'why': why})
    return resolve_sku.GemmaDisambiguator(client=lambda prompt: payload)


CANDS = [
    {'item_id': 'v1|100|0', 'title': 'Sony A7S III Body', 'price': '3498',
     'currency': 'USD', 'condition': 'New', 'epid': '', 'brand': 'Sony'},
    {'item_id': 'v1|200|0', 'title': 'Sony A7S III + 28-70 Kit', 'price': '3899',
     'currency': 'USD', 'condition': 'New', 'epid': '', 'brand': 'Sony'},
    {'item_id': 'v1|300|0', 'title': 'Battery grip for A7S III', 'price': '129',
     'currency': 'USD', 'condition': 'New', 'epid': '', 'brand': 'Meike'},
]


@pytest.fixture
def skus_path(tmp_path):
    """A spine pre-seeded with the proposal slug as an EXISTING registry entry
    (Q2: proposals are already registry entries the factory enriches)."""
    p = tmp_path / 'skus.json'
    p.write_text(json.dumps({
        '_description': 'test', 'version': '0.1.0', 'as_of': '2026-06-29',
        'skus': {
            'sony-a7s-iii': {
                'contamination_key': 'sony-a7s-iii',
                'vendor': 'Sony', 'model': 'A7S III', 'category': 'body',
                'aliases': ['ILCE-7SM3', 'a7siii'],
                # No identity block yet — that's exactly what the factory fills in.
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


# ── lookup_proposal (Q2: enrich, don't mint) ────────────────────────────────

def test_lookup_reads_existing_registry_identity(skus_path):
    t = resolve_sku.lookup_proposal('sony-a7s-iii', skus_path=skus_path)
    assert t['vendor'] == 'Sony'
    assert t['model'] == 'A7S III'
    assert t['category'] == 'body'
    assert t['label'] == 'Sony A7S III'
    assert 'ILCE-7SM3' in t['aliases']


def test_lookup_unknown_slug_raises(skus_path):
    with pytest.raises(resolve_sku.ResolveError):
        resolve_sku.lookup_proposal('canon-r5-ii', skus_path=skus_path)


# ── disambiguator ───────────────────────────────────────────────────────────

def test_disambiguator_confident_pick():
    g = _gemma(0, 0.95, 'bare body')
    out = g.pick({'vendor': 'Sony', 'model': 'A7S III', 'category': 'body'}, CANDS)
    assert out['index'] == 0
    assert out['confidence'] == 0.95
    assert out['item_id'] == 'v1|100|0'
    assert out['ranked'][0]['chosen'] is True
    assert out['ranked'][1]['chosen'] is False


def test_disambiguator_none_of_these():
    g = _gemma(-1, 0.0, 'no bare body present')
    out = g.pick({'vendor': 'Sony', 'model': 'A7S III', 'category': 'body'}, CANDS)
    assert out['index'] == -1
    assert out['item_id'] is None


def test_disambiguator_empty_candidates():
    g = _gemma(0, 0.9)
    out = g.pick({'vendor': 'Sony', 'model': 'X', 'category': 'body'}, [])
    assert out['item_id'] is None
    assert out['ranked'] == []


# ── resolve_proposal routing ────────────────────────────────────────────────

def test_route_confident_writes_spine(skus_path, queue_path, demand_path):
    ebay = MockEbay(candidates=CANDS)
    out = resolve_sku.resolve_proposal(
        'sony-a7s-iii', ebay=ebay, gemma=_gemma(0, 0.95),
        demand_log=__import__('demand_log'), review_queue=review_queue,
        floor=0.70, skus_path=skus_path,
        review_queue_path=queue_path, demand_log_path=demand_path)
    assert out['outcome'] == 'resolved'
    assert out['detail'] in ('created', 'updated')
    # The spine now carries the enriched identity for the proposal slug.
    reg = skus_registry.load_registry(skus_path)
    entry = reg['skus']['sony-a7s-iii']
    assert entry['identity']['legacy_item_id'] == 'v1|100|0'
    assert entry['category'] == 'body'
    # No review record, no demand event — a clean confident resolve.
    assert not queue_path.exists() or review_queue.load_pending(queue_path) == []


def test_route_low_confidence_enqueues_with_candidates(skus_path, queue_path, demand_path):
    ebay = MockEbay(candidates=CANDS)
    out = resolve_sku.resolve_proposal(
        'sony-a7s-iii', ebay=ebay, gemma=_gemma(0, 0.40),  # below floor
        demand_log=__import__('demand_log'), review_queue=review_queue,
        floor=0.70, skus_path=skus_path,
        review_queue_path=queue_path, demand_log_path=demand_path)
    assert out['outcome'] == 'queued'
    pend = review_queue.load_pending(queue_path)
    assert len(pend) == 1
    rec = pend[0]
    assert rec['reason'] == 'low_resolve_confidence'
    assert rec['candidates']  # the ranked competitors are frozen for /admin
    assert any(c['chosen'] for c in rec['candidates'])
    # Spine was NOT written — low-confidence does not enter the spine.
    reg = skus_registry.load_registry(skus_path)
    assert 'identity' not in reg['skus']['sony-a7s-iii'] or \
        not reg['skus']['sony-a7s-iii'].get('identity', {}).get('legacy_item_id')


def test_route_no_candidate_logs_demand(skus_path, queue_path, demand_path):
    import demand_log
    ebay = MockEbay(candidates=CANDS)
    out = resolve_sku.resolve_proposal(
        'sony-a7s-iii', ebay=ebay, gemma=_gemma(-1, 0.0),  # none-of-these
        demand_log=demand_log, review_queue=review_queue,
        floor=0.70, skus_path=skus_path,
        review_queue_path=queue_path, demand_log_path=demand_path)
    assert out['outcome'] == 'no_candidate'
    events = demand_log.read_events(demand_path)
    assert len(events) == 1
    assert events[0]['category'] == 'body'
    assert events[0]['identity'] is None  # unmet want, no resolved identity
    # Nothing entered the queue or the spine.
    assert not queue_path.exists() or review_queue.load_pending(queue_path) == []


def test_route_ebay_search_failure_propagates(skus_path, queue_path, demand_path):
    """A network blip is NOT 'no demand' — it must raise, not silently log unmet."""
    import demand_log
    ebay = MockEbay(search_raises=True)
    with pytest.raises(MockEbay.EbayAPIError):
        resolve_sku.resolve_proposal(
            'sony-a7s-iii', ebay=ebay, gemma=_gemma(0, 0.9),
            demand_log=demand_log, review_queue=review_queue,
            skus_path=skus_path, review_queue_path=queue_path,
            demand_log_path=demand_path)
    assert demand_log.read_events(demand_path) == []


def test_route_drops_candidates_without_item_id(skus_path, queue_path, demand_path):
    import demand_log
    bad = [{'title': 'no id row', 'price': '1'}]  # no item_id -> filtered out
    ebay = MockEbay(candidates=bad)
    out = resolve_sku.resolve_proposal(
        'sony-a7s-iii', ebay=ebay, gemma=_gemma(0, 0.9),
        demand_log=demand_log, review_queue=review_queue,
        skus_path=skus_path, review_queue_path=queue_path,
        demand_log_path=demand_path)
    # With every candidate filtered, Gemma sees none -> no_candidate.
    assert out['outcome'] == 'no_candidate'


# ── queue extension surface ─────────────────────────────────────────────────

def test_enqueue_rejects_bad_reason_override(queue_path):
    from slug_normalizer import SlugResolution
    res = SlugResolution(slug='x', source='generated', input_text='x',
                         needs_review=True, collision=None)
    with pytest.raises(ValueError):
        review_queue.enqueue(res, {'identity': {}}, 'V', 'M', 'body',
                             path=queue_path, reason_override='nonsense')


def test_existing_enqueue_unchanged_no_candidates(queue_path):
    """The slug-gate enqueue path still derives its reason and carries no
    candidates block — the extension is additive."""
    from slug_normalizer import SlugResolution
    res = SlugResolution(slug='sony-a7-iv', source='generated', input_text='Sony A7 IV',
                         needs_review=True, collision=None)
    rec = review_queue.enqueue(res, {'identity': {'epid': 'E'}}, 'Sony', 'A7 IV',
                               'body', path=queue_path)
    assert rec['reason'] == 'needs_review'
    assert 'candidates' not in rec


# ── re-resolve correction loop (within limits) ──────────────────────────────

def _seed_low_conf(skus_path, queue_path):
    ebay = MockEbay(candidates=CANDS)
    resolve_sku.resolve_proposal(
        'sony-a7s-iii', ebay=ebay, gemma=_gemma(0, 0.40),
        demand_log=__import__('demand_log'), review_queue=review_queue,
        floor=0.70, skus_path=skus_path, review_queue_path=queue_path)
    return review_queue.load_pending(queue_path)[0]


def test_reresolve_swaps_identity_to_chosen_candidate(skus_path, queue_path):
    rec = _seed_low_conf(skus_path, queue_path)
    qid = rec['queue_id']
    ebay = MockEbay(candidates=CANDS)
    # Human picks the SECOND candidate (the kit) — say that's the right product.
    updated = review_queue.reresolve(qid, 'v1|200|0', ebay=ebay, path=queue_path)
    assert updated['identity']['legacy_item_id'] == 'v1|200|0'
    assert updated['status'] == 'pending'  # NOT promoted — still gated
    chosen = [c for c in updated['candidates'] if c.get('chosen')]
    assert len(chosen) == 1 and chosen[0]['item_id'] == 'v1|200|0'
    assert chosen[0].get('human_chosen') is True
    assert ebay.resolve_calls == ['v1|200|0']  # a real round-trip happened


def test_reresolve_refuses_item_id_not_in_candidates(skus_path, queue_path):
    rec = _seed_low_conf(skus_path, queue_path)
    ebay = MockEbay(candidates=CANDS)
    with pytest.raises(ValueError):
        review_queue.reresolve(rec['queue_id'], 'v1|999|0', ebay=ebay, path=queue_path)
    # No eBay call for a rejected item_id — the guard fires before the round-trip.
    assert ebay.resolve_calls == []


def test_reresolve_refuses_non_low_confidence_record(queue_path):
    """A collision/needs_review record is a SLUG decision; re-resolve is refused."""
    from slug_normalizer import SlugResolution
    res = SlugResolution(slug='sony-a7-iv', source='generated', input_text='Sony A7 IV',
                         needs_review=True, collision=None)
    rec = review_queue.enqueue(res, {'identity': {'epid': 'E'}}, 'Sony', 'A7 IV',
                               'body', path=queue_path)
    ebay = MockEbay(candidates=CANDS)
    with pytest.raises(ValueError):
        review_queue.reresolve(rec['queue_id'], 'v1|100|0', ebay=ebay, path=queue_path)


def test_reresolve_then_promote_writes_corrected_identity(skus_path, queue_path):
    rec = _seed_low_conf(skus_path, queue_path)
    qid = rec['queue_id']
    ebay = MockEbay(candidates=CANDS)
    review_queue.reresolve(qid, 'v1|200|0', ebay=ebay, path=queue_path)
    # Now promote through the normal gate (empty spine apart from the proposal,
    # so the chosen slug doesn't collide).
    record, status = review_queue.promote(
        qid, 'sony-a7s-iii-kit', skus_path=skus_path, path=queue_path)
    reg = skus_registry.load_registry(skus_path)
    # The CORRECTED identity (candidate 2) is what reached the spine.
    assert reg['skus']['sony-a7s-iii-kit']['identity']['legacy_item_id'] == 'v1|200|0'


# ── admin render ────────────────────────────────────────────────────────────

def test_admin_renders_candidates_block(skus_path, queue_path):
    rec = _seed_low_conf(skus_path, queue_path)
    html = admin_surface._card_html(rec)
    assert 'candidates' in html
    assert 'use this listing' in html        # re-resolve buttons present
    assert 'machine pick' in html            # the chosen row is marked
    assert 'Sony A7S III Body' in html       # candidate titles rendered


def test_admin_collision_record_has_no_candidates_block(queue_path):
    from slug_normalizer import SlugResolution
    res = SlugResolution(slug='sony-a7-iv', source='generated', input_text='Sony A7 IV',
                         needs_review=True, collision='sony-a7iv')
    rec = review_queue.enqueue(res, {'identity': {'epid': 'E'}}, 'Sony', 'A7 IV',
                               'body', path=queue_path)
    html = admin_surface._card_html(rec)
    assert 'use this listing' not in html    # no candidates surface for slug-gate
