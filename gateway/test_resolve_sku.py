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
    assert entry['marketplace_ids']['ebay_legacy_item_id'] == 'v1|100|0'
    assert entry['facet'] == 'body'
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
    # READ THROUGH THE ACCESSOR. This previously asserted
    #   'identity' not in entry or not entry['identity']['legacy_item_id']
    # whose second clause went vacuous when the substrate migration moved
    # legacy_item_id -> marketplace_ids.ebay_legacy_item_id: a spine write in
    # the NEW shape carries an identity block (clause 1 false) with no
    # identity.legacy_item_id (clause 2 true), so the guard passed on exactly
    # the write it exists to forbid. get_marketplace_id checks marketplace_ids
    # first and falls back to the old identity path, so it catches a write in
    # EITHER shape — strictly stronger than the pair it replaces.
    reg = skus_registry.load_registry(skus_path)
    entry = reg['skus']['sony-a7s-iii']
    assert not skus_registry.get_marketplace_id(entry, 'ebay_legacy_item_id'), (
        "low-confidence resolve wrote a marketplace identity into the spine")


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
    assert reg['skus']['sony-a7s-iii-kit']['marketplace_ids'][
        'ebay_legacy_item_id'] == 'v1|200|0'


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


# ── MINTING WIRE (2026-06-30): enrich-or-mint, the demand→build connection ────
# These prove the previously-unbuilt wire: a proposal slug NOT in the registry is
# minted (not errored), routed per Lee's publish-air-gap decision — clean confident
# mint -> spine; collision -> review; low-conf -> review; no identity -> error.

def _mint_seed(tmp_path):
    """A spine that does NOT contain the proposal slug being minted, plus one
    existing entry so identity-freeze and collision can be exercised."""
    p = tmp_path / 'skus.json'
    p.write_text(json.dumps({
        '_description': 'test', 'version': '0.1.0', 'as_of': '2026-06-30',
        'skus': {
            'sigma-35-art-dg-dn-ii': {
                'contamination_key': 'sigma-35-art-dg-dn-ii',
                'vendor': 'Sigma', 'model': '35mm F1.4 Art DG DN II',
                'category': 'lens',
            },
        },
    }))
    return p


def test_lookup_or_mint_existing_is_enrich_not_mint(skus_path):
    """An existing slug returns the enrich identity, source='resolved', minted=False
    — byte-for-byte the historical lookup_proposal behaviour."""
    t = resolve_sku.lookup_or_mint('sony-a7s-iii', skus_path=skus_path)
    assert t['source'] == 'resolved'
    assert t['minted'] is False
    assert t['resolution'] is None
    assert t['vendor'] == 'Sony' and t['category'] == 'body'


def test_lookup_or_mint_new_slug_mints_with_identity(tmp_path):
    """A registry MISS with vendor+model mints a fresh generated slug, flagged
    minted + needs review, category empty (eBay-derived downstream)."""
    skus = _mint_seed(tmp_path)
    t = resolve_sku.lookup_or_mint('canon-r5-ii', vendor='Canon', model='R5 II',
                                   skus_path=skus)
    assert t['minted'] is True
    assert t['source'] == 'generated'
    assert t['slug'] == 'canon-r5-ii'
    assert t['category'] == ''           # not known until eBay resolves it
    assert t['resolution'] is not None and t['resolution'].needs_review is True


def test_lookup_or_mint_miss_without_identity_raises(tmp_path):
    """A registry miss with NO vendor/model cannot mint — loud ResolveError, never
    a silent skip (the demand signal must not be lost)."""
    skus = _mint_seed(tmp_path)
    with pytest.raises(resolve_sku.ResolveError):
        resolve_sku.lookup_or_mint('canon-r5-ii', skus_path=skus)


def test_lookup_or_mint_frozen_by_identity_is_enrich(tmp_path):
    """vendor/model that already has an entry (under a possibly hand-authored slug)
    resolves to that FROZEN slug as an enrich — not a second mint."""
    skus = _mint_seed(tmp_path)
    t = resolve_sku.lookup_or_mint(
        'sigma-whatever', vendor='Sigma', model='35mm F1.4 Art DG DN II',
        skus_path=skus)
    assert t['slug'] == 'sigma-35-art-dg-dn-ii'
    assert t['minted'] is False and t['source'] == 'resolved'


def test_route_mint_clean_confident_writes_spine_with_provenance(tmp_path, queue_path, demand_path):
    """The headline path: a NEW slug, confident pick, clean (no collision) ->
    spine write carrying source='generated' + minted_needs_review. This is what
    yesterday errored 10/10; it now builds. The publish air-gap is the review."""
    skus = _mint_seed(tmp_path)
    # eBay returns a body-category item (88433 -> 'body' in the map) so category
    # is derived cleanly from marketplace truth.
    ebay = MockEbay(
        candidates=[{'item_id': 'v1|900|0', 'title': 'Canon EOS R5 Mark II Body',
                     'price': '4299', 'currency': 'USD', 'condition': 'New',
                     'epid': '', 'brand': 'Canon'}],
        resolve_map={'v1|900|0': {
            'identity': {'epid': 'EP-R5II', 'legacy_item_id': 'v1|900|0',
                         'ebay_category_id': '88433', 'brand': 'Canon',
                         'mpn': '', 'market_title': 'Canon EOS R5 Mark II Body',
                         'image': 'https://img/r5ii.jpg',
                         'price_seen': {'value': '4299', 'currency': 'USD', 'as_of': 'now'}},
            'affiliate_url': 'https://ebay/itm/v1|900|0?campid=5339138080'}})
    out = resolve_sku.resolve_proposal(
        'canon-r5-ii', ebay=ebay, gemma=_gemma(0, 0.95),
        vendor='Canon', model='R5 II',
        demand_log=__import__('demand_log'), review_queue=review_queue,
        floor=0.70, skus_path=skus,
        review_queue_path=queue_path, demand_log_path=demand_path)
    assert out['outcome'] == 'resolved'
    assert out['source'] == 'generated' and out['minted'] is True
    assert out['category'] == 'body'              # derived from ebay 88433
    assert out['minted_needs_review'] is True     # generated -> review at publish
    # The spine now has a fresh entry under the minted slug with provenance.
    reg = skus_registry.load_registry(skus)
    entry = reg['skus']['canon-r5-ii']
    assert entry['source'] == 'generated'
    assert entry['minted_needs_review'] is True
    assert entry['facet'] == 'body'
    assert entry['marketplace_ids']['ebay_legacy_item_id'] == 'v1|900|0'
    # No review record — a clean mint goes straight through to the publish gate.
    assert not queue_path.exists() or review_queue.load_pending(queue_path) == []


def test_route_mint_unknown_category_still_writes_but_flags_review(tmp_path, queue_path, demand_path):
    """A confident mint whose eBay category id is UNKNOWN writes the spine (cards
    cost ~nothing; the publish gate reviews) but category='' and needs_review
    stays True so Lee fills the blank category at publish."""
    skus = _mint_seed(tmp_path)
    ebay = MockEbay(
        candidates=[{'item_id': 'v1|901|0', 'title': 'DJI RS 4 Gimbal',
                     'price': '549', 'currency': 'USD', 'condition': 'New',
                     'epid': '', 'brand': 'DJI'}],
        resolve_map={'v1|901|0': {
            'identity': {'epid': '', 'legacy_item_id': 'v1|901|0',
                         'ebay_category_id': '99999999',  # unmapped
                         'brand': 'DJI', 'mpn': '',
                         'market_title': 'DJI RS 4 Gimbal', 'image': '',
                         'price_seen': {'value': '549', 'currency': 'USD', 'as_of': 'now'}},
            'affiliate_url': 'https://ebay/itm/v1|901|0?campid=5339138080'}})
    out = resolve_sku.resolve_proposal(
        'dji-rs-4', ebay=ebay, gemma=_gemma(0, 0.95),
        vendor='DJI', model='RS 4',
        demand_log=__import__('demand_log'), review_queue=review_queue,
        floor=0.70, skus_path=skus,
        review_queue_path=queue_path, demand_log_path=demand_path)
    assert out['outcome'] == 'resolved'
    assert out['category'] == ''                  # unknown id -> abstain, no guess
    assert out['minted_needs_review'] is True
    reg = skus_registry.load_registry(skus)
    assert reg['skus']['dji-rs-4']['facet'] == ''
    assert reg['skus']['dji-rs-4']['minted_needs_review'] is True


def test_route_mint_collision_goes_to_review_before_ebay(tmp_path, queue_path, demand_path):
    """A minted slug that normalizes the same as an existing spine slug (the Sigma
    class) routes to review_queue reason='collision' BEFORE eBay is touched — the
    one duplicate hazard the publish eyeball won't reliably catch. The spine is
    NOT written; eBay search is never called."""
    skus = _mint_seed(tmp_path)
    ebay = MockEbay(candidates=CANDS)  # if reached, search_calls would be non-empty
    out = resolve_sku.resolve_proposal(
        'sigma-35-art-dgdn-ii', ebay=ebay, gemma=_gemma(0, 0.99),
        vendor='Sigma', model='35 art dgdn ii',
        demand_log=__import__('demand_log'), review_queue=review_queue,
        floor=0.70, skus_path=skus,
        review_queue_path=queue_path, demand_log_path=demand_path)
    assert out['outcome'] == 'queued'
    assert out['reason'] == 'collision'
    assert out['collision_with'] == 'sigma-35-art-dg-dn-ii'
    # eBay was never hit — the collision short-circuits before search.
    assert ebay.search_calls == []
    assert ebay.resolve_calls == []
    # Review record carries the collision badge data.
    pend = review_queue.load_pending(queue_path)
    assert len(pend) == 1
    assert pend[0]['reason'] == 'collision'
    assert pend[0]['collision_with'] == 'sigma-35-art-dg-dn-ii'
    # Spine got no new entry — only the original seed remains.
    reg = skus_registry.load_registry(skus)
    assert set(reg['skus']) == {'sigma-35-art-dg-dn-ii'}


def test_route_mint_low_confidence_goes_to_review(tmp_path, queue_path, demand_path):
    """A minted slug with a LOW-confidence eBay pick routes to review just like an
    enrich low-conf — reason low_resolve_confidence, candidates frozen, no spine
    write. Mint and enrich share the resolve-time review path."""
    skus = _mint_seed(tmp_path)
    ebay = MockEbay(
        candidates=[{'item_id': 'v1|902|0', 'title': 'Canon R5 II (maybe?)',
                     'price': '4000', 'currency': 'USD', 'condition': 'Used',
                     'epid': '', 'brand': 'Canon'}],
        resolve_map={'v1|902|0': {
            'identity': {'epid': '', 'legacy_item_id': 'v1|902|0',
                         'ebay_category_id': '88433', 'brand': 'Canon', 'mpn': '',
                         'market_title': 'Canon R5 II', 'image': '',
                         'price_seen': {'value': '4000', 'currency': 'USD', 'as_of': 'now'}},
            'affiliate_url': 'https://ebay/itm/v1|902|0'}})
    out = resolve_sku.resolve_proposal(
        'canon-r5-ii', ebay=ebay, gemma=_gemma(0, 0.40),  # below floor
        vendor='Canon', model='R5 II',
        demand_log=__import__('demand_log'), review_queue=review_queue,
        floor=0.70, skus_path=skus,
        review_queue_path=queue_path, demand_log_path=demand_path)
    assert out['outcome'] == 'queued'
    pend = review_queue.load_pending(queue_path)
    assert len(pend) == 1 and pend[0]['reason'] == 'low_resolve_confidence'
    # No spine entry for the minted slug — low-conf never writes the spine.
    reg = skus_registry.load_registry(skus)
    assert 'canon-r5-ii' not in reg['skus']


# --- live _generate payload + resilience (the 2026-08-22 Qwen3/schema swap) ----
class _FakeResp:
    def __init__(self, body):
        self._body = body.encode("utf-8")
    def read(self):
        return self._body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_generate_sends_schema_constrained_qwen_payload(monkeypatch):
    """The live disambiguation POST rides the verdict schema in `format` (forced
    choice) and the Qwen3 model — the change that stops the ramble that aborted
    the resolve batch. Guards against a regression to unconstrained output."""
    seen = {}
    def fake_urlopen(req, timeout=None):
        seen["payload"] = json.loads(req.data.decode("utf-8"))
        seen["timeout"] = timeout
        return _FakeResp(json.dumps({"response": '{"index": 1, "confidence": 0.9, "why": "ok"}'}))
    monkeypatch.setattr(resolve_sku.urllib.request, "urlopen", fake_urlopen)

    gemma = resolve_sku.GemmaDisambiguator(client=None, timeout=99)
    raw = gemma._generate("PROMPT")

    assert seen["payload"]["format"] == resolve_sku._VERDICT_SCHEMA
    assert "Qwen3" in seen["payload"]["model"]
    assert seen["payload"]["options"]["temperature"] == 0.0
    assert seen["timeout"] == 99
    assert json.loads(raw)["index"] == 1


def test_generate_retries_once_on_transient_timeout(monkeypatch):
    """A single cold-load/timeout blip is retried once (the first call warms the
    model); it must not surface as a batch-aborting error."""
    import socket as _socket
    calls = {"n": 0}
    def flaky_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _socket.timeout("cold load")
        return _FakeResp(json.dumps({"response": '{"index": 0, "confidence": 1.0, "why": "warm"}'}))
    monkeypatch.setattr(resolve_sku.urllib.request, "urlopen", flaky_urlopen)

    gemma = resolve_sku.GemmaDisambiguator(client=None)
    raw = gemma._generate("PROMPT")
    assert calls["n"] == 2
    assert json.loads(raw)["why"] == "warm"


# ── mint-time contamination-join gate (structural, 2026-08-27) ───────────────

def _contam_file(tmp_path, keys):
    p = tmp_path / 'contamination.json'
    p.write_text(json.dumps({'products': {k: {} for k in keys}}))
    return p


def test_mint_unresolved_contamination_routes_to_review(
        skus_path, queue_path, demand_path, tmp_path, monkeypatch):
    # A NEW slug whose contamination_key resolves to NOTHING (no specific, no
    # canon-generic) must NOT enter the spine silently-broken — it routes to review.
    monkeypatch.setenv('ASKMADDI_CONTAMINATION_JSON',
                       str(_contam_file(tmp_path, ['sony-generic'])))
    ebay = MockEbay(candidates=CANDS)
    out = resolve_sku.resolve_proposal(
        'canon-eos-r100', vendor='Canon', model='EOS R100',
        ebay=ebay, gemma=_gemma(0, 0.95),
        demand_log=__import__('demand_log'), review_queue=review_queue,
        floor=0.70, skus_path=skus_path,
        review_queue_path=queue_path, demand_log_path=demand_path)
    assert out['outcome'] == 'queued'
    assert out['reason'] == 'no_contamination_entry'
    # spine was NOT written for the dangling mint
    assert 'canon-eos-r100' not in skus_registry.load_registry(skus_path)['skus']


def test_mint_resolves_to_brand_generic_and_records_tier(
        skus_path, queue_path, demand_path, tmp_path, monkeypatch):
    # canon-generic present -> the mint resolves (coarse) and stores the resolved
    # key + a coverage-debt tier, instead of the dangling self-name.
    monkeypatch.setenv('ASKMADDI_CONTAMINATION_JSON',
                       str(_contam_file(tmp_path, ['canon-generic'])))
    ebay = MockEbay(candidates=CANDS)
    out = resolve_sku.resolve_proposal(
        'canon-eos-r100', vendor='Canon', model='EOS R100',
        ebay=ebay, gemma=_gemma(0, 0.95),
        demand_log=__import__('demand_log'), review_queue=review_queue,
        floor=0.70, skus_path=skus_path,
        review_queue_path=queue_path, demand_log_path=demand_path)
    assert out['outcome'] == 'resolved'
    entry = skus_registry.load_registry(skus_path)['skus']['canon-eos-r100']
    assert entry['contamination_key'] == 'canon-generic'
    assert entry['contamination_tier'] == 'brand_generic'


def test_mint_specific_entry_stores_slug_no_tier(
        skus_path, queue_path, demand_path, tmp_path, monkeypatch):
    # If a specific entry was pre-authored (the ideal flow), the slug is stored
    # verbatim and no coverage-debt tier is recorded.
    monkeypatch.setenv('ASKMADDI_CONTAMINATION_JSON',
                       str(_contam_file(tmp_path, ['canon-eos-r100'])))
    ebay = MockEbay(candidates=CANDS)
    out = resolve_sku.resolve_proposal(
        'canon-eos-r100', vendor='Canon', model='EOS R100',
        ebay=ebay, gemma=_gemma(0, 0.95),
        demand_log=__import__('demand_log'), review_queue=review_queue,
        floor=0.70, skus_path=skus_path,
        review_queue_path=queue_path, demand_log_path=demand_path)
    assert out['outcome'] == 'resolved'
    entry = skus_registry.load_registry(skus_path)['skus']['canon-eos-r100']
    assert entry['contamination_key'] == 'canon-eos-r100'
    assert 'contamination_tier' not in entry


# ── Cross-slug product-identity dedup gate (spec: maddi-multisource-identity-
#    matcher; live proof: Autel EVO II Pro / Skydio 2 drone dups resurrecting) ──

def _dedup_seed(tmp_path, *, built_mpn='102000410', built_vendor='Autel',
                built_model='EVO II Pro', facet='drone'):
    """A spine holding ONE already-built entry with a real product MPN, under a
    slug the incoming proposal will NOT share (so the same-slug rebind firewall
    can't see it — only the cross-slug identity gate can)."""
    p = tmp_path / 'skus.json'
    p.write_text(json.dumps({
        '_description': 'test', 'version': '0.1.0', 'as_of': '2026-09-01',
        'skus': {
            'built-canonical': {
                'contamination_key': 'built-canonical',
                'vendor': built_vendor, 'model': built_model, 'facet': facet,
                'gtin': None,
                'marketplace_ids': {'ebay_epid': '', 'ebay_legacy_item_id': 'OLD-1',
                                    'amazon_asin': None},
                'identity': {'mpn': built_mpn, 'brand': built_vendor,
                             'market_title': f'{built_vendor} {built_model}'},
            },
        },
    }))
    return p


def _ebay_resolving_to(item_id, *, mpn, brand, title, cat='99999999'):
    """A MockEbay whose single candidate resolves to the given product identity."""
    return MockEbay(
        candidates=[{'item_id': item_id, 'title': title, 'price': '999',
                     'currency': 'USD', 'condition': 'New', 'epid': '', 'brand': brand}],
        resolve_map={item_id: {
            'identity': {'epid': '', 'legacy_item_id': item_id,
                         'ebay_category_id': cat, 'brand': brand, 'mpn': mpn,
                         'market_title': title, 'image': '',
                         'price_seen': {'value': '999', 'currency': 'USD', 'as_of': 'now'}},
            'affiliate_url': f'https://ebay/itm/{item_id}?campid=5339138080'}})


def test_dedup_clean_dup_drops_no_mint(tmp_path, queue_path, demand_path):
    """A second listing of an already-built product (distinct item_id, SAME MPN,
    same product family) mints a novel title-slug but the identity gate drops it:
    outcome 'duplicate_identity', no spine write, no review record."""
    skus = _dedup_seed(tmp_path)
    # A second LISTING of the same product: curated 'EVO II Pro' fully present,
    # extras are only listing noise + the SKU code -> clean dup, auto-drop.
    ebay = _ebay_resolving_to('NEW-2', mpn='102000410', brand='Autel',
                              title='Autel EVO II Pro 102000410 New Sealed USA In Stock 2-4 Shipping')
    out = resolve_sku.resolve_proposal(
        'autel-evo-ii-pro-listing2', ebay=ebay, gemma=_gemma(0, 0.95),
        vendor='Autel', model='EVO II Pro 102000410 New Sealed USA In Stock 2-4 Shipping',
        demand_log=__import__('demand_log'), review_queue=review_queue,
        floor=0.70, skus_path=skus,
        review_queue_path=queue_path, demand_log_path=demand_path)
    assert out['outcome'] == 'duplicate_identity'
    assert out['dup_of'] == 'built-canonical'
    reg = skus_registry.load_registry(skus)['skus']
    assert 'autel-evo-ii-pro-listing2' not in reg          # never minted
    assert not queue_path.exists() or review_queue.load_pending(queue_path) == []


def test_dedup_misstamp_shared_id_routes_to_review(tmp_path, queue_path, demand_path):
    """Same MPN but a DIFFERENT product family (the live ILCE7RM5B on both a7r and
    a7-v) is a contradiction, not a dup: never auto-merge, route to /admin so the
    mis-stamp is adjudicated by a human. No silent drop of a possibly-real product."""
    skus = _dedup_seed(tmp_path, built_mpn='ILCE7RM5B', built_vendor='Sony',
                       built_model='a7R', facet='body')
    ebay = _ebay_resolving_to('NEW-9', mpn='ILCE7RM5B', brand='Sony',
                              title='Sony a7 V Body')
    out = resolve_sku.resolve_proposal(
        'sony-a7-v', ebay=ebay, gemma=_gemma(0, 0.95),
        vendor='Sony', model='a7 V',
        demand_log=__import__('demand_log'), review_queue=review_queue,
        floor=0.70, skus_path=skus,
        review_queue_path=queue_path, demand_log_path=demand_path)
    assert out['outcome'] == 'queued'
    assert out['reason'] == 'duplicate_identity_contradiction'
    assert out['dup_of'] == 'built-canonical'
    assert 'sony-a7-v' not in skus_registry.load_registry(skus)['skus']  # not minted
    assert len(review_queue.load_pending(queue_path)) == 1               # human sees it


def test_dedup_placeholder_mpn_is_not_a_join_key(tmp_path, queue_path, demand_path):
    """Two DIFFERENT drones both carrying the seller placeholder 'Dose not apply'
    (the live Avata 2 / Mavic 4 Pro pair) must NOT dedup — a placeholder is never
    identity. The second product mints normally instead of being swallowed."""
    skus = _dedup_seed(tmp_path, built_mpn='Dose not apply', built_vendor='DJI',
                       built_model='Avata 2', facet='drone')
    ebay = _ebay_resolving_to('NEW-7', mpn='Dose not apply', brand='DJI',
                              title='DJI Mavic 4 Pro USA In Stock', cat='99999999')
    out = resolve_sku.resolve_proposal(
        'dji-mavic-4-pro', ebay=ebay, gemma=_gemma(0, 0.95),
        vendor='DJI', model='Mavic 4 Pro',
        demand_log=__import__('demand_log'), review_queue=review_queue,
        floor=0.70, skus_path=skus,
        review_queue_path=queue_path, demand_log_path=demand_path)
    assert out['outcome'] == 'resolved'                    # minted, NOT deduped
    assert 'dji-mavic-4-pro' in skus_registry.load_registry(skus)['skus']


def test_dedup_gate_is_mint_only(skus_path, queue_path, demand_path):
    """An ENRICH (existing canonical slug) is never dedup-dropped even though its
    id may match spine entries — the gate guards the mint path only, because only
    a mint can create a NEW duplicate slug."""
    ebay = MockEbay(candidates=CANDS)  # resolves default identity mpn ILCE-7SM3
    out = resolve_sku.resolve_proposal(
        'sony-a7s-iii', ebay=ebay, gemma=_gemma(0, 0.95),
        demand_log=__import__('demand_log'), review_queue=review_queue,
        floor=0.70, skus_path=skus_path,
        review_queue_path=queue_path, demand_log_path=demand_path)
    assert out['outcome'] == 'resolved'
    assert out.get('minted') is not True


def test_model_family_agrees_distinguishes_dups_from_variants():
    """The dedup 'same product?' arbiter: clean dups agree (drop), while
    successors / variants / mis-stamps disagree (route to human)."""
    f = resolve_sku._model_family_agrees
    # clean dups -> agree (SKU code / listing noise are not product differences)
    assert f('Skydio', 'Skydio 2', 'Skydio', 'Skydio 2 SDRC2V1 New USA')
    assert f('DJI', 'Avata 2', 'DJI', 'DJI Avata 2 Fly Smart USA In Stock 2-4 Shipping')
    # successors / variants / mis-stamps / cross-brand -> disagree
    assert not f('Canon', 'R6', 'Canon', 'EOS R6 II')          # generation marker 'ii'
    assert not f('DJI', 'RS 3', 'DJI', 'RS 3 Pro')             # variant marker 'pro'
    assert not f('DJI', 'Avata 2', 'DJI', 'Avata 3')           # different model number
    assert not f('Sony', 'a7R', 'Sony', 'a7 V')               # not contained
    assert not f('DJI', 'Avata 2', 'Autel', 'Avata 2')        # cross-brand
