"""Tests for the catalog-image second pass (option 2, 2026-07-15).

Doctrine under test:
  1. Precision bias: token firewall gates every candidate; epid-less
     candidates never inspected; own-epid candidates outrank others.
  2. Human-terminal + upgrade-only: overrides.image_thumb and existing
     image_catalog block both the selector and the writer.
  3. The rescue is pure (no writes); set_image_catalog is surgical (no
     upsert churn); the sweep composes the two and is dry-run by default.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / 'tools'))

import image_secondpass as isp  # noqa: E402
import skus_registry as sr      # noqa: E402
import image_catalog_sweep as sweep  # noqa: E402


# ─── Fake eBay (duck-typed, gtin-suite style) ────────────────────────────────
class FakeEbay:
    def __init__(self, candidates=None, resolves=None, search_exc=None):
        self.candidates = candidates or []
        self.resolves = resolves or {}          # item_id -> identity dict
        self.search_exc = search_exc
        self.search_calls = []
        self.resolve_calls = []

    def search_candidates(self, query, limit=10):
        self.search_calls.append(query)
        if self.search_exc:
            raise self.search_exc
        return self.candidates

    def resolve(self, item_id, customid=None):
        self.resolve_calls.append(item_id)
        r = self.resolves.get(item_id)
        if isinstance(r, Exception):
            raise r
        return {'identity': r or {}}


def _cand(item_id, title, epid='e100'):
    return {'item_id': item_id, 'title': title, 'epid': epid,
            'price': '', 'currency': '', 'condition': '', 'brand': ''}


def _entry(brand='Sony', mpn='ILCE-7CM2', image_catalog='', override=None,
           epid=''):
    e = {'label': 'Sony A7C II',
         'identity': {'brand': brand, 'mpn': mpn,
                      'image_catalog': image_catalog}}
    if epid:
        e['identity']['epid'] = epid
    if override:
        e['overrides'] = {'image_thumb': override}
    return e


CAT_URL = 'https://i.ebayimg.com/images/g/stock/s-l1600.jpg'


# ─── rescue_catalog_image ─────────────────────────────────────────────────────
def test_rescue_happy_path():
    ebay = FakeEbay(
        candidates=[_cand('v1|1|0', 'Sony Alpha A7C II ILCE-7CM2 Body')],
        resolves={'v1|1|0': {'image_catalog': CAT_URL}})
    res = isp.rescue_catalog_image('sony-a7c-ii', _entry(), ebay=ebay, sleep_s=0)
    assert res['verdict'] == isp.RESCUED
    assert res['image_catalog'] == CAT_URL
    prov = res['image_provenance']
    assert prov['recovered_by'] == 'image-second-pass'
    assert prov['winner']['item_id'] == 'v1|1|0'


def test_identity_firewall_blocks_wrong_product():
    # Clean stock image of the WRONG product must never win. v2: the check
    # runs AFTER resolve (summaries can't carry mpn — the gtin lesson), so
    # the fetch is spent but the image is rejected with evidence.
    ebay = FakeEbay(
        candidates=[_cand('v1|1|0', 'Sony Alpha A7 IV Body')],  # no token in title
        resolves={'v1|1|0': {'image_catalog': CAT_URL, 'mpn': 'ILCE-7M4'}})
    res = isp.rescue_catalog_image('sony-a7c-ii', _entry(mpn='ILCE-7CM2'),
                                   ebay=ebay, sleep_s=0)
    assert res['verdict'] == isp.NO_CATALOG_FOUND
    assert ebay.resolve_calls == ['v1|1|0']
    assert res['inspected'][0]['rejected'] == 'identity-firewall'


def test_resolved_mpn_satisfies_firewall_when_title_lacks_token():
    # THE 0/14 BUG: seller titles say 'Sony a7 IV', not 'ILCE-7M4'. The token
    # must be allowed to match getItem's identity.mpn, not just the title.
    ebay = FakeEbay(
        candidates=[_cand('v1|1|0', 'Sony a7 IV Mirrorless Camera Body')],
        resolves={'v1|1|0': {'image_catalog': CAT_URL, 'mpn': 'ILCE-7M4'}})
    res = isp.rescue_catalog_image('sony-a7iv', _entry(mpn='ILCE-7M4'),
                                   ebay=ebay, sleep_s=0)
    assert res['verdict'] == isp.RESCUED
    assert res['image_catalog'] == CAT_URL


def test_epid_equality_accepts_without_any_mpn():
    # Keyless entries (Peak Design veterans): no mpn anywhere, but the entry
    # carries its own epid — epid equality IS the identity check.
    ebay = FakeEbay(
        candidates=[_cand('v1|1|0', 'Peak Design Travel Tripod CF', epid='e42')],
        resolves={'v1|1|0': {'image_catalog': CAT_URL, 'mpn': ''}})
    entry = {'identity': {'brand': 'Peak Design', 'mpn': '',
                          'market_title': 'Peak Design Travel Tripod Carbon',
                          'image_catalog': '', 'epid': 'e42'}}
    res = isp.rescue_catalog_image('peak-design-travel-tripod', entry,
                                   ebay=ebay, sleep_s=0)
    assert res['verdict'] == isp.RESCUED
    assert res['image_provenance']['epid_match'] is True


def test_no_mpn_no_epid_match_fails_closed():
    ebay = FakeEbay(
        candidates=[_cand('v1|1|0', 'Peak Design Travel Tripod CF', epid='e42')],
        resolves={'v1|1|0': {'image_catalog': CAT_URL, 'mpn': ''}})
    entry = {'identity': {'brand': 'Peak Design', 'mpn': '',
                          'market_title': 'Peak Design Travel Tripod',
                          'image_catalog': ''}}   # no epid to equal
    res = isp.rescue_catalog_image('peak-design-travel-tripod', entry,
                                   ebay=ebay, sleep_s=0)
    assert res['verdict'] == isp.NO_CATALOG_FOUND
    assert res['inspected'][0]['rejected'] == 'identity-firewall'


def test_query_falls_back_to_market_title_then_slug():
    # market_title fallback (the NO-KEYS trio fix)
    ebay = FakeEbay(candidates=[])
    entry = {'identity': {'brand': '', 'mpn': '',
                          'market_title': 'Peak Design Travel Tripod Carbon',
                          'image_catalog': ''}}
    res = isp.rescue_catalog_image('peak-design-travel-tripod', entry, ebay=ebay)
    assert res['query'] == 'Peak Design Travel Tripod Carbon'
    # slug floor: bare entry still gets a look
    ebay2 = FakeEbay(candidates=[])
    res2 = isp.rescue_catalog_image(
        'manfrotto-befree', {'identity': {'image_catalog': ''}}, ebay=ebay2)
    assert res2['query'] == 'manfrotto befree'


def test_epidless_candidates_skipped():
    ebay = FakeEbay(
        candidates=[_cand('v1|1|0', 'Sony A7C II ILCE-7CM2', epid='')])
    res = isp.rescue_catalog_image('s', _entry(), ebay=ebay, sleep_s=0)
    assert res['verdict'] == isp.NO_CANDIDATES
    assert ebay.resolve_calls == []


def test_own_epid_candidate_outranks_others():
    ebay = FakeEbay(
        candidates=[
            _cand('v1|other|0', 'Sony A7C II ILCE-7CM2 kit', epid='e999'),
            _cand('v1|mine|0', 'Sony A7C II ILCE-7CM2 body', epid='e777'),
        ],
        resolves={'v1|mine|0': {'image_catalog': CAT_URL},
                  'v1|other|0': {'image_catalog': 'https://i.ebayimg.com/o.jpg'}})
    res = isp.rescue_catalog_image('s', _entry(epid='e777'), ebay=ebay, sleep_s=0)
    assert res['verdict'] == isp.RESCUED
    assert res['image_provenance']['winner']['item_id'] == 'v1|mine|0'
    assert res['image_provenance']['epid_match'] is True


def test_dead_candidate_falls_through_to_next():
    ebay = FakeEbay(
        candidates=[
            _cand('v1|dead|0', 'Sony A7C II ILCE-7CM2', epid='e1'),
            _cand('v1|live|0', 'Sony A7C II ILCE-7CM2', epid='e2'),
        ],
        resolves={'v1|dead|0': RuntimeError('listing ended'),
                  'v1|live|0': {'image_catalog': CAT_URL}})
    res = isp.rescue_catalog_image('s', _entry(), ebay=ebay, sleep_s=0)
    assert res['verdict'] == isp.RESCUED
    assert any(i.get('error') for i in res['image_provenance']['inspected'])


def test_existing_catalog_and_override_short_circuit():
    ebay = FakeEbay()
    assert isp.rescue_catalog_image(
        's', _entry(image_catalog=CAT_URL), ebay=ebay)['verdict'] == isp.HAS_CATALOG
    assert isp.rescue_catalog_image(
        's', _entry(override='https://x/y.jpg'), ebay=ebay)['verdict'] == isp.SKIPPED_OVERRIDE
    assert ebay.search_calls == []           # zero network on skips


def test_no_keys_verdict():
    # NO_KEYS is now nearly unreachable (slug floor), but an empty slug +
    # bare identity still fails closed rather than searching for nothing.
    e = {'identity': {}}
    assert isp.rescue_catalog_image('', e, ebay=FakeEbay())['verdict'] == isp.NO_KEYS


def test_search_failure_is_reported_not_raised():
    ebay = FakeEbay(search_exc=RuntimeError('HTTP 503'))
    res = isp.rescue_catalog_image('s', _entry(), ebay=ebay, sleep_s=0)
    assert res['verdict'].startswith(isp.SEARCH_FAILED)


# ─── set_image_catalog writer ─────────────────────────────────────────────────
def _seed_registry(tmp_path, entry, slug='sony-a7c-ii'):
    p = tmp_path / 'skus.json'
    p.write_text(json.dumps({'skus': {slug: entry}}), encoding='utf-8')
    return p


def test_writer_writes_url_and_provenance(tmp_path):
    p = _seed_registry(tmp_path, _entry())
    prov = {'recovered_by': 'image-second-pass'}
    assert sr.set_image_catalog('sony-a7c-ii', CAT_URL, prov, path=p) == 'written'
    ident = sr.load_registry(p)['skus']['sony-a7c-ii']['identity']
    assert ident['image_catalog'] == CAT_URL
    assert ident['image_provenance'] == prov


def test_writer_upgrade_only(tmp_path):
    p = _seed_registry(tmp_path, _entry(image_catalog='https://i.ebayimg.com/first.jpg'))
    assert sr.set_image_catalog('sony-a7c-ii', CAT_URL, path=p) == 'skipped-has-catalog'
    ident = sr.load_registry(p)['skus']['sony-a7c-ii']['identity']
    assert ident['image_catalog'] == 'https://i.ebayimg.com/first.jpg'


def test_writer_human_terminal(tmp_path):
    p = _seed_registry(tmp_path, _entry(override='https://curated.jpg'))
    assert sr.set_image_catalog('sony-a7c-ii', CAT_URL, path=p) == 'skipped-override'


def test_writer_guards(tmp_path):
    p = _seed_registry(tmp_path, _entry())
    assert sr.set_image_catalog('ghost', CAT_URL, path=p) == 'missing-slug'
    assert sr.set_image_catalog('sony-a7c-ii', '', path=p) == 'bad-url'
    assert sr.set_image_catalog('sony-a7c-ii', '   ', path=p) == 'bad-url'


# ─── sweep runner ─────────────────────────────────────────────────────────────
def _sweep_registry(tmp_path):
    entries = {
        'needs-one': _entry(),
        'has-one': _entry(image_catalog=CAT_URL),
        'curated': _entry(override='https://curated.jpg'),
    }
    p = tmp_path / 'skus.json'
    p.write_text(json.dumps({'skus': entries}), encoding='utf-8')
    return p


def test_sweep_dry_run_selects_only_needy_and_writes_nothing(tmp_path):
    p = _sweep_registry(tmp_path)
    ebay = FakeEbay(candidates=[_cand('v1|1|0', 'Sony A7C II ILCE-7CM2')],
                    resolves={'v1|1|0': {'image_catalog': CAT_URL}})
    summary = sweep.run(ebay=ebay, registry_path=p, sleep_s=0, out=lambda *_: None)
    assert list(summary['per_slug']) == ['needs-one']   # skips has-one + curated
    assert summary['rescued'] == 1 and summary['written'] == 0
    ident = sr.load_registry(p)['skus']['needs-one']['identity']
    assert ident['image_catalog'] == ''                 # dry-run: untouched


def test_sweep_commit_writes(tmp_path):
    p = _sweep_registry(tmp_path)
    ebay = FakeEbay(candidates=[_cand('v1|1|0', 'Sony A7C II ILCE-7CM2')],
                    resolves={'v1|1|0': {'image_catalog': CAT_URL}})
    summary = sweep.run(ebay=ebay, registry_path=p, commit=True, sleep_s=0,
                        out=lambda *_: None)
    assert summary['written'] == 1
    ident = sr.load_registry(p)['skus']['needs-one']['identity']
    assert ident['image_catalog'] == CAT_URL
    assert ident['image_provenance']['recovered_by'] == 'image-second-pass'


def test_sweep_single_slug_and_missing_slug(tmp_path):
    p = _sweep_registry(tmp_path)
    ebay = FakeEbay(candidates=[_cand('v1|1|0', 'Sony A7C II ILCE-7CM2')],
                    resolves={'v1|1|0': {'image_catalog': CAT_URL}})
    s = sweep.run(slug='needs-one', ebay=ebay, registry_path=p, sleep_s=0,
                  out=lambda *_: None)
    assert s['per_slug'] == {'needs-one': isp.RESCUED}
    s = sweep.run(slug='ghost', ebay=ebay, registry_path=p, out=lambda *_: None)
    assert s.get('error') == 'missing-slug'
