"""
Tests for gtin_secondpass + skus_registry.set_gtin — the L2 wire (step 5b).
========================================================================
Offline only: no live eBay, no network. The centerpiece is the 7-SKU FIXTURE
pinning the admission gate against the verdicts the 2026-07-01 live probe
established (3 admit, 2 conflict-drop, 1 wrong-match-drop, 1 no-gtin-drop) —
the frozen-agreement discipline the substrate spec mandates. Candidate shapes
mirror what the probe actually observed on the box.
"""
import json

import pytest

import gtin_secondpass as sp
import skus_registry


def cand(gtin=None, source='product.gtins', title='', mpn='', item_id='v1|x|0',
         epid='e', error=None):
    c = {'item_id': item_id, 'epid': epid, 'title': title, 'mpn': mpn,
         'gtin': gtin, 'chosen_source': source if gtin else None}
    if error:
        c['error'] = error
        c['gtin'] = None
        c['chosen_source'] = None
    return c


# ─── THE 7-SKU FIXTURE (probe-derived, frozen) ──────────────────────────────
# Each case: (slug, model_token, candidates, expected_verdict, expected_gtin)

G_A7IV = '00027242920569'
G_A1 = '00027242921658'
G_A7V = '00027242926783'
G_PDPRO_A = '00857757007801'
G_PDPRO_B = '00857757007818'
G_PDTRAV_A = '00857757007429'
G_PDTRAV_B = '00857757007436'
G_A7CR = '00027242925403'

FIXTURE = [
    # 3 ADMIT — multi-candidate, agreeing, catalog-sourced, token-matched.
    ('sony-a7iv', 'ILCE-7M4',
     [cand(G_A7IV, title='Sony Alpha a7 IV Mirrorless Camera Body ILCE-7M4'),
      cand(G_A7IV, title='Sony a7IV Full Frame Camera', mpn='ILCE-7M4')],
     sp.ADMIT, G_A7IV),

    ('sony-a1', 'ILCE-1',
     [cand(G_A1, title='Sony Alpha 1 Mirrorless Camera ILCE-1'),
      cand(G_A1, title='Sony A1 Body', mpn='ILCE-1'),
      cand(G_A1, title='Sony Alpha 1 50MP', mpn='ILCE-1')],
     sp.ADMIT, G_A1),

    ('sony-a7-v', 'ILCE-7M5',
     [cand(G_A7V, title='Sony a7 V Mirrorless Camera ILCE-7M5'),
      cand(G_A7V, title='Sony Alpha 7V Body', mpn='ILCE-7M5')],
     sp.ADMIT, G_A7V),

    # 2 CONFLICT-DROP — Peak Design tripods: two GENUINE SKUs each, candidates
    # disagree. Never silent-pick; null + conflict receipt -> /admin.
    ('peak-design-pro-tripod', 'PRO-TRIPOD',
     [cand(G_PDPRO_A, title='Peak Design Pro Tripod Carbon'),
      cand(G_PDPRO_B, title='Peak Design Pro Tripod Aluminum')],
     sp.CONFLICT_DROP, None),

    ('peak-design-travel-tripod', 'TT-CB-5-150-AL-1',
     [cand(G_PDTRAV_A, title='Peak Design Travel Tripod Carbon Fiber'),
      cand(G_PDTRAV_B, title='Peak Design Travel Tripod Aluminum'),
      cand(G_PDTRAV_A, title='Peak Design Travel Tripod CF')],
     sp.CONFLICT_DROP, None),

    # 1 SINGLE-CANDIDATE-DROP — the load-bearing a7CR finding: the brand+mpn
    # search fuzzy-matched a DIFFERENT camera; one candidate cannot
    # self-certify (the conflict check needs >=2 to catch a wrong product).
    ('sony-a7r', 'ILCE-7RM5',
     [cand(G_A7CR, title='Sony a7CR Compact Full Frame Camera ILCE-7CR')],
     sp.SINGLE_CANDIDATE_DROP, None),

    # 1 NO-GTIN-DROP — ulanzi: catalog-associated results exist, none carry
    # product.gtins.
    ('ulanzi-f38', 'F38',
     [cand(None, title='Ulanzi F38 Quick Release Plate'),
      cand(None, title='Ulanzi F38 QR System')],
     sp.NO_GTIN_DROP, None),
]


@pytest.mark.parametrize('slug,token,cands,want_verdict,want_gtin',
                         FIXTURE, ids=[f[0] for f in FIXTURE])
def test_probe_fixture_verdicts_pinned(slug, token, cands, want_verdict, want_gtin):
    gtin, verdict, receipt = sp.admission_gate(cands, token)
    assert verdict == want_verdict
    assert gtin == want_gtin
    # receipt is audit-complete: every candidate recorded
    assert len(receipt['candidates']) == len(cands)


def test_fixture_tallies_match_probe_summary():
    """3 admit, 2 conflict-drop, 1 wrong-match-drop, 1 no-gtin-drop."""
    verdicts = [sp.admission_gate(c, t)[1] for _, t, c, _, _ in FIXTURE]
    assert verdicts.count(sp.ADMIT) == 3
    assert verdicts.count(sp.CONFLICT_DROP) == 2
    assert verdicts.count(sp.SINGLE_CANDIDATE_DROP) == 1
    assert verdicts.count(sp.NO_GTIN_DROP) == 1


# ─── Gate clauses not exercised by the probe set ─────────────────────────────

def test_clause3_aspect_only_source_drops():
    # Two agreeing candidates, but BOTH aspect-sourced (seller free-text,
    # format-validated but not product-matched) — clause 3 drops.
    cands = [cand(G_A7IV, source='aspect', title='Sony ILCE-7M4 body'),
             cand(G_A7IV, source='aspect', title='Sony a7IV ILCE-7M4')]
    gtin, verdict, _ = sp.admission_gate(cands, 'ILCE-7M4')
    assert verdict == sp.SOURCE_DROP
    assert gtin is None


def test_clause3_one_catalog_source_among_agreeing_passes():
    cands = [cand(G_A7IV, source='product.gtins', title='Sony ILCE-7M4 body'),
             cand(G_A7IV, source='aspect', title='Sony a7IV')]
    gtin, verdict, _ = sp.admission_gate(cands, 'ILCE-7M4')
    assert verdict == sp.ADMIT


def test_clause4_agreeing_wrong_product_pair_drops():
    # The scenario clause 4 exists for: TWO a7CR listings agree and would
    # pass clauses 1-3 — but neither matches the a7R's model token.
    cands = [cand(G_A7CR, title='Sony a7CR Compact Camera ILCE-7CR'),
             cand(G_A7CR, title='Sony Alpha 7CR Body', mpn='ILCE-7CR')]
    gtin, verdict, _ = sp.admission_gate(cands, 'ILCE-7RM5')
    assert verdict == sp.TOKEN_DROP
    assert gtin is None


def test_clause4_token_match_normalizes_hyphens_and_case():
    # Seller title says 'ILCE7M4' (no hyphen) — must still match 'ILCE-7M4'.
    cands = [cand(G_A7IV, title='SONY ilce7m4 mirrorless'),
             cand(G_A7IV, title='Sony Alpha a7 IV', mpn='ilce-7m4')]
    gtin, verdict, _ = sp.admission_gate(cands, 'ILCE-7M4')
    assert verdict == sp.ADMIT


def test_empty_model_token_fails_closed():
    cands = [cand(G_A7IV, title='Sony a7 IV'), cand(G_A7IV, title='Sony a7IV')]
    gtin, verdict, _ = sp.admission_gate(cands, '')
    assert verdict == sp.TOKEN_DROP


def test_majority_never_silent_picks():
    # 2-vs-1 disagreement is CONFLICT, not a vote.
    cands = [cand(G_A7IV, title='Sony ILCE-7M4'),
             cand(G_A7IV, title='Sony a7IV ILCE-7M4'),
             cand(G_A7CR, title='Sony a7CR')]
    gtin, verdict, _ = sp.admission_gate(cands, 'ILCE-7M4')
    assert verdict == sp.CONFLICT_DROP
    assert gtin is None


def test_errored_candidates_count_toward_nothing():
    cands = [cand(G_A7IV, title='Sony ILCE-7M4'),
             cand(error='HTTP 500', title='Sony a7IV')]
    gtin, verdict, _ = sp.admission_gate(cands, 'ILCE-7M4')
    assert verdict == sp.SINGLE_CANDIDATE_DROP  # only one USABLE bearer


# ─── recover_gtin wire (fake ebay, no network) ───────────────────────────────

class FakeEbay:
    def __init__(self, summaries, resolves):
        self.summaries = summaries          # search_candidates() rows
        self.resolves = resolves            # item_id -> resolve() result | Exception
        self.resolve_calls = []

    def search_candidates(self, query, limit=10):
        return self.summaries

    def resolve(self, item_id, customid=None):
        self.resolve_calls.append(item_id)
        r = self.resolves[item_id]
        if isinstance(r, Exception):
            raise r
        return r


def _resolve_payload(gtin, source, title, mpn=''):
    return {'identity': {'gtin': gtin, 'mpn': mpn, 'market_title': title,
                         'gtin_provenance': {'chosen_source': source if gtin else None,
                                             'conflict': False}},
            'affiliate_url': 'https://ebay.com/itm/x', '_raw': {}}


def test_recover_gtin_admit_end_to_end():
    fake = FakeEbay(
        summaries=[
            {'item_id': 'v1|1|0', 'epid': 'e1', 'title': 'Sony a7IV ILCE-7M4'},
            {'item_id': 'v1|2|0', 'epid': '', 'title': 'a7IV battery (accessory)'},
            {'item_id': 'v1|3|0', 'epid': 'e3', 'title': 'Sony Alpha a7 IV Body'},
        ],
        resolves={
            'v1|1|0': _resolve_payload(G_A7IV, 'product.gtins', 'Sony a7IV ILCE-7M4'),
            'v1|3|0': _resolve_payload(G_A7IV, 'product.gtins',
                                       'Sony Alpha a7 IV Body', mpn='ILCE-7M4'),
        })
    res = sp.recover_gtin('Sony', 'ILCE-7M4', ebay=fake, sleep_s=0)
    assert res['verdict'] == sp.ADMIT
    assert res['gtin'] == G_A7IV
    # non-ePID row was never resolved (only catalog-assoc pay the resolve cost)
    assert fake.resolve_calls == ['v1|1|0', 'v1|3|0']
    prov = res['gtin_provenance']
    assert prov['chosen_source'] == 'secondpass:product.gtins'  # never mistakable for L1
    assert prov['conflict'] is False
    assert prov['recovery']['method'] == 'ebay-secondpass'
    assert prov['recovery']['query'] == 'Sony ILCE-7M4'


def test_recover_gtin_resolve_error_recorded_not_fatal():
    fake = FakeEbay(
        summaries=[{'item_id': 'v1|1|0', 'epid': 'e1', 'title': 'Sony a7IV ILCE-7M4'},
                   {'item_id': 'v1|2|0', 'epid': 'e2', 'title': 'Sony a7 IV'}],
        resolves={'v1|1|0': _resolve_payload(G_A7IV, 'product.gtins',
                                             'Sony a7IV ILCE-7M4'),
                  'v1|2|0': RuntimeError('getItem failed: HTTP 500')})
    res = sp.recover_gtin('Sony', 'ILCE-7M4', ebay=fake, sleep_s=0)
    # errored candidate counts toward nothing -> single usable bearer -> drop
    assert res['verdict'] == sp.SINGLE_CANDIDATE_DROP
    errs = [c for c in res['gtin_provenance']['recovery']['candidates']
            if c.get('error')]
    assert len(errs) == 1


def test_recover_gtin_no_keys():
    res = sp.recover_gtin('', '', model='', ebay=FakeEbay([], {}))
    assert res['verdict'] == sp.NO_KEYS
    assert res['gtin'] is None


def test_recover_gtin_falls_back_to_brand_model_query():
    fake = FakeEbay(summaries=[], resolves={})
    res = sp.recover_gtin('Ulanzi', '', model='F38', ebay=fake)
    assert res['query'] == 'Ulanzi F38'
    assert res['verdict'] == sp.NO_CANDIDATES


def test_recover_gtin_search_failure_is_verdict_not_raise():
    class Boom:
        def search_candidates(self, query, limit=10):
            raise RuntimeError('browse search failed: HTTP 500')
    res = sp.recover_gtin('Sony', 'ILCE-7M4', ebay=Boom())
    assert res['verdict'].startswith(sp.SEARCH_FAILED)
    assert res['gtin'] is None


# ─── recover_own_listing (L1-first step) ─────────────────────────────────────

class OwnListingEbay:
    """resolve()-only fake keyed on the reconstructed v1|legacy|0 id."""
    def __init__(self, resolves):
        self.resolves = resolves
        self.resolve_calls = []
        self.search_calls = []

    def resolve(self, item_id, customid=None):
        self.resolve_calls.append(item_id)
        r = self.resolves[item_id]
        if isinstance(r, Exception):
            raise r
        return r

    def search_candidates(self, query, limit=10):
        self.search_calls.append(query)
        return []


def test_own_listing_l1_recovers_with_unmodified_l1_provenance_plus_audit():
    l1_prov = {'chosen_source': 'product.gtins', 'conflict': False,
               'observations': [{'source': 'product.gtins', 'raw': G_A7IV,
                                 'gtin14': G_A7IV, 'valid': True,
                                 'identifier_type': 'GTIN'}]}
    fake = OwnListingEbay({'v1|555|0': {
        'identity': {'gtin': G_A7IV, 'gtin_provenance': l1_prov},
        'affiliate_url': '', '_raw': {}}})
    res = sp.recover_own_listing('555', ebay=fake)
    assert res['verdict'] == sp.OWN_LISTING_L1
    assert res['gtin'] == G_A7IV
    prov = res['gtin_provenance']
    # L1 shape unmodified — NOT secondpass-namespaced (same trust as mint)
    assert prov['chosen_source'] == 'product.gtins'
    assert prov['observations'] == l1_prov['observations']
    # additive audit key: backfilled always distinguishable from minted
    assert prov['recovered_by'] == 'sweep-own-listing'
    assert prov['recovered_at'].endswith('Z')
    assert fake.resolve_calls == ['v1|555|0']  # probe's exact reconstruction


def test_own_listing_l1_internal_conflict_persists_as_is():
    # Mint-uniform: an L1 conflict flag rides through untouched.
    fake = OwnListingEbay({'v1|555|0': {
        'identity': {'gtin': G_A7IV,
                     'gtin_provenance': {'chosen_source': 'product.gtins',
                                         'conflict': True}},
        'affiliate_url': '', '_raw': {}}})
    res = sp.recover_own_listing('555', ebay=fake)
    assert res['verdict'] == sp.OWN_LISTING_L1
    assert res['gtin_provenance']['conflict'] is True


def test_own_listing_bare_and_dead_and_no_legacy():
    bare = OwnListingEbay({'v1|1|0': {'identity': {'gtin': None},
                                      'affiliate_url': '', '_raw': {}}})
    assert sp.recover_own_listing('1', ebay=bare)['verdict'] == sp.OWN_LISTING_BARE

    dead = OwnListingEbay({'v1|2|0': RuntimeError('getItem failed: HTTP 404')})
    v = sp.recover_own_listing('2', ebay=dead)['verdict']
    assert v.startswith(sp.OWN_LISTING_DEAD)  # expected path, not an error

    assert sp.recover_own_listing('', ebay=bare)['verdict'] == sp.NO_LEGACY_ID
    assert sp.recover_own_listing(None, ebay=bare)['verdict'] == sp.NO_LEGACY_ID


# ─── sweep recover(): L1-first ordering + fall-through ───────────────────────

def _tool():
    import sys
    sys.path.insert(0, 'tools')
    import secondpass_gtin
    return secondpass_gtin


def test_sweep_own_listing_wins_search_never_called():
    tool = _tool()
    fake = OwnListingEbay({'v1|777|0': {
        'identity': {'gtin': G_A1,
                     'gtin_provenance': {'chosen_source': 'product.gtins',
                                         'conflict': False}},
        'affiliate_url': '', '_raw': {}}})
    entry = {'model': 'a1', 'identity': {'brand': 'Sony', 'mpn': 'ILCE-1',
                                         'legacy_item_id': '777'}}
    res = tool.recover(entry, ebay=fake)
    assert res['verdict'] == sp.OWN_LISTING_L1
    assert res['gtin'] == G_A1
    assert fake.search_calls == []  # gate never consulted; no search paid


def test_sweep_falls_through_to_second_pass_on_dead_listing():
    tool = _tool()

    class Fake:
        def __init__(self):
            self.search_calls = []

        def resolve(self, item_id, customid=None):
            if item_id == 'v1|888|0':          # own listing: sold/ended
                raise RuntimeError('getItem failed: HTTP 404')
            return _resolve_payload(G_A7IV, 'product.gtins',
                                    'Sony a7IV ILCE-7M4', mpn='ILCE-7M4')

        def search_candidates(self, query, limit=10):
            self.search_calls.append(query)
            return [{'item_id': 'v1|1|0', 'epid': 'e1', 'title': 'Sony a7IV ILCE-7M4'},
                    {'item_id': 'v1|2|0', 'epid': 'e2', 'title': 'Sony a7 IV ILCE-7M4'}]

    fake = Fake()
    entry = {'model': 'a7 IV', 'identity': {'brand': 'Sony', 'mpn': 'ILCE-7M4',
                                            'legacy_item_id': '888'}}
    res = tool.recover(entry, ebay=fake)
    assert res['verdict'] == sp.ADMIT           # second pass ran and admitted
    assert res['own_listing'].startswith(sp.OWN_LISTING_DEAD)  # auditable fall-through
    assert fake.search_calls == ['Sony ILCE-7M4']


def test_sweep_no_legacy_id_goes_straight_to_second_pass():
    tool = _tool()
    fake = FakeEbay(summaries=[], resolves={})
    entry = {'model': 'F38', 'identity': {'brand': 'Ulanzi', 'mpn': 'F38'}}
    res = tool.recover(entry, ebay=fake)
    assert res['own_listing'] == sp.NO_LEGACY_ID
    assert res['verdict'] == sp.NO_CANDIDATES




@pytest.fixture
def tmp_registry(tmp_path):
    path = tmp_path / 'skus.json'
    path.write_text(json.dumps({'as_of': '2026-07-01', 'skus': {
        'sony-a7iv': {'vendor': 'Sony', 'model': 'a7 IV',
                      'identity': {'brand': 'Sony', 'mpn': 'ILCE-7M4'}},  # pre-L1: no gtin key
        'sigma-35-art-dg-dn-ii': {'vendor': 'Sigma', 'model': '35mm',
                                  'identity': {'gtin': None}},            # L1-null
        'canon-r5': {'vendor': 'Canon', 'model': 'R5',
                     'identity': {'gtin': '00013803323114'}},             # L1-set
    }}))
    return str(path)


def test_set_gtin_writes_null_and_absent_key_entries(tmp_registry):
    prov = {'chosen_source': 'secondpass:product.gtins', 'conflict': False}
    assert skus_registry.set_gtin('sony-a7iv', G_A7IV, prov,
                                  path=tmp_registry) == 'written'
    assert skus_registry.set_gtin('sigma-35-art-dg-dn-ii', '00085126340698', prov,
                                  path=tmp_registry) == 'written'
    reg = json.load(open(tmp_registry))
    assert reg['skus']['sony-a7iv']['identity']['gtin'] == G_A7IV
    assert reg['skus']['sony-a7iv']['identity']['gtin_provenance'] == prov


def test_set_gtin_never_overwrites_l1(tmp_registry):
    assert skus_registry.set_gtin('canon-r5', G_A7IV, {},
                                  path=tmp_registry) == 'skipped-has-gtin'
    reg = json.load(open(tmp_registry))
    assert reg['skus']['canon-r5']['identity']['gtin'] == '00013803323114'


def test_set_gtin_missing_slug(tmp_registry):
    assert skus_registry.set_gtin('nope', G_A7IV, {},
                                  path=tmp_registry) == 'missing-slug'


def test_set_gtin_persists_conflict_receipt_with_null_gtin(tmp_registry):
    # CONFLICT-DROP: null anchor + conflict-flagged provenance for /admin.
    prov = {'chosen_source': None, 'conflict': True, 'observations': []}
    assert skus_registry.set_gtin('sony-a7iv', None, prov,
                                  path=tmp_registry) == 'written'
    idn = json.load(open(tmp_registry))['skus']['sony-a7iv']['identity']
    assert idn['gtin'] is None
    assert idn['gtin_provenance']['conflict'] is True
