"""
Route-level tests for /ebay/resolve in app_production.
========================================================================
Offline only: ebay_api.resolve is monkeypatched, so no live credentials and
no network. These pin the HTTP surface of the wire flag — guard ordering,
param parsing, the `raw` toggle, and EbayAPIError → 502 — distinct from the
unit-level resolve() coverage in test_ebay_api.py.

The guard-first ordering matters: an unconfigured gateway must return 503
before it inspects item_id, so a caller can't distinguish "no creds" from
"bad input" against a cold gateway.
"""
import importlib

import pytest


@pytest.fixture
def app_mod():
    import app_production
    importlib.reload(app_production)
    return app_production


@pytest.fixture
def client(app_mod):
    return app_mod.app.test_client()


def _configure(monkeypatch, app_mod, configured=True):
    """Force the HAS_EBAY_API / is_configured() guard to a known state."""
    monkeypatch.setattr(app_mod, 'HAS_EBAY_API', True)
    monkeypatch.setattr(app_mod.ebay_api, 'is_configured', lambda: configured)


# ─── guard ordering: unconfigured returns 503 before item_id parsing ────────

def test_resolve_unconfigured_returns_503_even_with_item_id(
        monkeypatch, app_mod, client):
    _configure(monkeypatch, app_mod, configured=False)
    r = client.get('/ebay/resolve?item_id=v1|123|0')
    assert r.status_code == 503
    assert r.get_json()['error'] == 'eBay API not configured'


def test_resolve_unconfigured_returns_503_with_no_item_id(
        monkeypatch, app_mod, client):
    # Guard precedes validation: no creds → 503, never 400.
    _configure(monkeypatch, app_mod, configured=False)
    r = client.get('/ebay/resolve')
    assert r.status_code == 503


# ─── input validation (configured) ─────────────────────────────────────────

def test_resolve_configured_missing_item_id_returns_400(
        monkeypatch, app_mod, client):
    _configure(monkeypatch, app_mod, configured=True)
    r = client.get('/ebay/resolve')
    assert r.status_code == 400
    assert r.get_json()['error'] == 'missing item_id'


# ─── happy path: identity + affiliate_url, _raw omitted by default ──────────

def _fake_result():
    return {
        'identity': {'epid': '15042899333', 'title': 'Sony A7 IV Body'},
        'affiliate_url': 'https://www.ebay.com/itm/123?campid=5339138080',
        '_raw': {'itemId': 'v1|123|0', 'big': 'payload'},
    }


def test_resolve_default_omits_raw(monkeypatch, app_mod, client):
    _configure(monkeypatch, app_mod, configured=True)
    captured = {}

    def _fake_resolve(item_id, customid=None):
        captured['item_id'] = item_id
        captured['customid'] = customid
        return _fake_result()

    monkeypatch.setattr(app_mod.ebay_api, 'resolve', _fake_resolve)

    r = client.get('/ebay/resolve?item_id=v1|123|0&customid=sony-a7iv')
    assert r.status_code == 200
    body = r.get_json()
    assert body['identity']['epid'] == '15042899333'
    assert body['affiliate_url'].startswith('https://www.ebay.com/itm/123')
    assert '_raw' not in body            # heavy payload not echoed by default
    # params threaded through to resolve()
    assert captured['item_id'] == 'v1|123|0'
    assert captured['customid'] == 'sony-a7iv'


def test_resolve_raw_flag_includes_raw(monkeypatch, app_mod, client):
    _configure(monkeypatch, app_mod, configured=True)
    monkeypatch.setattr(app_mod.ebay_api, 'resolve',
                        lambda item_id, customid=None: _fake_result())
    r = client.get('/ebay/resolve?item_id=v1|123|0&raw=1')
    assert r.status_code == 200
    assert r.get_json()['_raw'] == {'itemId': 'v1|123|0', 'big': 'payload'}


def test_resolve_blank_customid_becomes_none(monkeypatch, app_mod, client):
    _configure(monkeypatch, app_mod, configured=True)
    captured = {}

    def _fake_resolve(item_id, customid=None):
        captured['customid'] = customid
        return _fake_result()

    monkeypatch.setattr(app_mod.ebay_api, 'resolve', _fake_resolve)
    client.get('/ebay/resolve?item_id=v1|123|0&customid=')
    assert captured['customid'] is None   # empty string normalized to None


# ─── error path: EbayAPIError → 502, no secret/item_id leak in body ─────────

def test_resolve_ebay_error_returns_502(monkeypatch, app_mod, client):
    _configure(monkeypatch, app_mod, configured=True)

    def _boom(item_id, customid=None):
        raise app_mod.ebay_api.EbayAPIError('getItem failed: HTTP 404')

    monkeypatch.setattr(app_mod.ebay_api, 'resolve', _boom)
    r = client.get('/ebay/resolve?item_id=v1|999|0')
    assert r.status_code == 502
    assert r.get_json()['error'] == 'getItem failed: HTTP 404'


# ═══════════════════════════════════════════════════════════════════════════
# Capture mode (Phase 3) — the read-only route becomes the live demand WRITER.
# ═══════════════════════════════════════════════════════════════════════════
# Two writes, strict order, both OUTSIDE the skus.json spine:
#   1. demand_log.log_unmet  — UNCONDITIONAL on every capture tap.
#   2. review_queue.enqueue  — ONLY when the slug gate trips (ambiguous).
# Capture NEVER writes skus.json. These tests isolate both stores to tmp_path
# and drive slug_normalizer.resolve_slug to controlled outcomes, so the routing
# logic (which write fires when) is pinned without a live skus.json or network.

from slug_normalizer import SlugResolution


@pytest.fixture
def isolated_stores(monkeypatch, app_mod, tmp_path):
    """Redirect demand_log + review_queue writes into tmp_path.

    Subtlety: the modules bind their default path arg at def-time
    (enqueue(..., path=REVIEW_QUEUE_PATH)), so patching the module CONSTANT
    does not retroactively redirect an already-bound default. We instead wrap
    the two writer functions the route calls so they inject the tmp path, and
    read back through the same tmp paths. Returns (demand_path, queue_path).
    """
    demand_path = tmp_path / 'demand_log.jsonl'
    queue_path = tmp_path / 'review_queue.json'

    _real_log = app_mod.demand_log.log_unmet
    _real_enqueue = app_mod.review_queue.enqueue

    def _log_tmp(category, identity=None, **kw):
        return _real_log(category, identity=identity, path=demand_path)

    def _enqueue_tmp(resolution, resolved, vendor, model, category, **kw):
        return _real_enqueue(resolution, resolved, vendor, model, category,
                             path=queue_path)

    monkeypatch.setattr(app_mod.demand_log, 'log_unmet', _log_tmp)
    monkeypatch.setattr(app_mod.review_queue, 'enqueue', _enqueue_tmp)
    return demand_path, queue_path


def _capture_ready(monkeypatch, app_mod):
    """Configured eBay + capture modules present, resolve() stubbed to fixture."""
    _configure(monkeypatch, app_mod, configured=True)
    monkeypatch.setattr(app_mod, 'HAS_CAPTURE', True)
    monkeypatch.setattr(app_mod.ebay_api, 'resolve',
                        lambda item_id, customid=None: _fake_result())


def _stub_slug(monkeypatch, app_mod, resolution):
    """Pin slug_normalizer.resolve_slug to a known SlugResolution."""
    monkeypatch.setattr(app_mod.slug_normalizer, 'resolve_slug',
                        lambda vendor, model, **kw: resolution)


_CAPTURE_QS = ('item_id=v1|123|0&capture=1'
               '&vendor=Sony&model=A7%20IV&category=body')


# ─── capture preflight: requires vendor/model/category, before the API call ──

def test_capture_missing_vendor_returns_400_before_resolve(
        monkeypatch, app_mod, client, isolated_stores):
    _capture_ready(monkeypatch, app_mod)
    called = {'resolve': False}

    def _tracking_resolve(item_id, customid=None):
        called['resolve'] = True
        return _fake_result()

    monkeypatch.setattr(app_mod.ebay_api, 'resolve', _tracking_resolve)
    r = client.get('/ebay/resolve?item_id=v1|123|0&capture=1'
                   '&model=A7%20IV&category=body')
    assert r.status_code == 400
    assert 'vendor' in r.get_json()['error']
    # preflight must fail BEFORE the billable resolve() round-trip
    assert called['resolve'] is False


def test_capture_lists_all_missing_human_identity_fields(
        monkeypatch, app_mod, client, isolated_stores):
    _capture_ready(monkeypatch, app_mod)
    r = client.get('/ebay/resolve?item_id=v1|123|0&capture=1')
    body = r.get_json()
    assert r.status_code == 400
    for field in ('vendor', 'model', 'category'):
        assert field in body['error']


def test_capture_unavailable_when_modules_absent_returns_503(
        monkeypatch, app_mod, client):
    _configure(monkeypatch, app_mod, configured=True)
    monkeypatch.setattr(app_mod, 'HAS_CAPTURE', False)
    r = client.get('/ebay/resolve?' + _CAPTURE_QS)
    assert r.status_code == 503
    assert r.get_json()['error'] == 'capture not available'


# ─── clean resolution: demand logged, NOTHING enqueued ──────────────────────

def test_capture_clean_logs_demand_but_does_not_enqueue(
        monkeypatch, app_mod, client, isolated_stores):
    _capture_ready(monkeypatch, app_mod)
    # A frozen/clean slug — source override, no review, no collision.
    _stub_slug(monkeypatch, app_mod, SlugResolution(
        slug='sony-a7iv', source='override', input_text='Sony A7 IV',
        needs_review=False, collision=None))
    demand_path, queue_path = isolated_stores

    r = client.get('/ebay/resolve?' + _CAPTURE_QS)
    assert r.status_code == 200
    cap = r.get_json()['capture']
    assert cap['demand_logged'] is True
    assert cap['queued'] is None          # clean → no review record
    assert cap['needs_review'] is False
    assert cap['slug'] == 'sony-a7iv'
    # demand store has exactly one event; queue store was never written.
    assert app_mod.demand_log.read_events(demand_path)
    assert not queue_path.exists()


# ─── ambiguous resolution: demand logged AND enqueued ───────────────────────

def test_capture_needs_review_enqueues_and_logs_demand(
        monkeypatch, app_mod, client, isolated_stores):
    _capture_ready(monkeypatch, app_mod)
    _stub_slug(monkeypatch, app_mod, SlugResolution(
        slug='peak-design-pro-tripod', source='generated',
        input_text='Peak Design Pro Tripod',
        needs_review=True, collision=None))
    demand_path, queue_path = isolated_stores

    r = client.get('/ebay/resolve?item_id=v1|123|0&capture=1'
                   '&vendor=Peak%20Design&model=Pro%20Tripod&category=support')
    assert r.status_code == 200
    cap = r.get_json()['capture']
    assert cap['demand_logged'] is True
    assert cap['queued'] is not None       # ambiguous → queued for review
    assert cap['needs_review'] is True
    # both stores written; queued record is pending and outside the spine.
    assert app_mod.demand_log.read_events(demand_path)
    pending = app_mod.review_queue.load_pending(queue_path)
    assert len(pending) == 1
    assert pending[0]['queue_id'] == cap['queued']
    assert pending[0]['status'] == 'pending'


def test_capture_collision_enqueues_as_needs_review(
        monkeypatch, app_mod, client, isolated_stores):
    _capture_ready(monkeypatch, app_mod)
    # A generated slug that normalizes onto an existing different spine slug —
    # the sony-a7iv ~ sony-a7-iv silent-join class. Must enqueue.
    _stub_slug(monkeypatch, app_mod, SlugResolution(
        slug='sony-a7-iv', source='generated', input_text='Sony A7 IV',
        needs_review=False, collision='sony-a7iv'))
    _demand_path, queue_path = isolated_stores

    r = client.get('/ebay/resolve?' + _CAPTURE_QS)
    cap = r.get_json()['capture']
    assert cap['queued'] is not None
    assert cap['needs_review'] is True      # collision alone trips review
    pending = app_mod.review_queue.load_pending(queue_path)
    assert len(pending) == 1
    assert pending[0]['collision_with'] == 'sony-a7iv'


# ─── idempotency: the same unmet product captured twice → one queue record ──

def test_capture_twice_dedups_to_one_queue_record(
        monkeypatch, app_mod, client, isolated_stores):
    _capture_ready(monkeypatch, app_mod)
    _stub_slug(monkeypatch, app_mod, SlugResolution(
        slug='peak-design-pro-tripod', source='generated',
        input_text='Peak Design Pro Tripod',
        needs_review=True, collision=None))
    _demand_path, queue_path = isolated_stores

    qs = ('item_id=v1|123|0&capture=1'
          '&vendor=Peak%20Design&model=Pro%20Tripod&category=support')
    first = client.get('/ebay/resolve?' + qs).get_json()['capture']
    second = client.get('/ebay/resolve?' + qs).get_json()['capture']
    # enqueue is keyed on vendor|model|epid — same product → same record id.
    assert first['queued'] == second['queued']
    assert len(app_mod.review_queue.load_pending(queue_path)) == 1


# ─── capture NEVER touches skus.json (spine purity) ─────────────────────────

def test_capture_never_writes_skus_spine(
        monkeypatch, app_mod, client, isolated_stores):
    _capture_ready(monkeypatch, app_mod)
    _stub_slug(monkeypatch, app_mod, SlugResolution(
        slug='peak-design-pro-tripod', source='generated',
        input_text='Peak Design Pro Tripod',
        needs_review=True, collision=None))
    # upsert is the ONLY spine writer — if capture ever calls it, fail loudly.
    def _forbidden_upsert(*a, **kw):
        raise AssertionError('capture must NOT write skus.json; '
                             'promotion is the human-authorized path')
    monkeypatch.setattr(app_mod.review_queue.skus_registry,
                        'upsert', _forbidden_upsert)
    r = client.get('/ebay/resolve?item_id=v1|123|0&capture=1'
                   '&vendor=Peak%20Design&model=Pro%20Tripod&category=support')
    assert r.status_code == 200   # enqueue happened, upsert did not


# ─── read-only path unchanged when capture absent (no regression) ───────────

def test_no_capture_param_leaves_payload_without_capture_block(
        monkeypatch, app_mod, client, isolated_stores):
    _capture_ready(monkeypatch, app_mod)
    r = client.get('/ebay/resolve?item_id=v1|123|0&customid=sony-a7iv')
    body = r.get_json()
    assert r.status_code == 200
    assert 'capture' not in body          # opt-in only
    assert body['identity']['epid'] == '15042899333'
