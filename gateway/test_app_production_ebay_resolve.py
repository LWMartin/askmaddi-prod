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
