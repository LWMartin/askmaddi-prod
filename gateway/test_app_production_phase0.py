"""
Route-level tests for the Phase 0 distribution seams in app_production:
/ping event routing (persist vs legacy print-only) and /subscribe.
========================================================================
Offline only: analytics_log.log_event and subscribers.add are monkeypatched
at the app_production-imported module objects, so nothing touches data/.
These pin the HTTP surface — event routing, honeypot silence, the no-oracle
success shape — distinct from the unit coverage in test_analytics_log.py
and test_subscribers.py.
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


# ─── /ping event routing ────────────────────────────────────────────────────

def test_ping_outbound_persists(monkeypatch, app_mod, client):
    calls = []
    monkeypatch.setattr(app_mod.analytics_log, 'log_event',
                        lambda event, **kw: calls.append((event, kw)) or {})
    r = client.post('/ping', json={'event': 'outbound', 'category': 'camera',
                                   'retailer': 'amazon'})
    assert r.status_code == 200
    assert r.get_json() == {'received': True}
    assert calls == [('outbound', {'category': 'camera',
                                   'retailer': 'amazon', 'engine': None})]


def test_ping_ai_referral_persists(monkeypatch, app_mod, client):
    calls = []
    monkeypatch.setattr(app_mod.analytics_log, 'log_event',
                        lambda event, **kw: calls.append((event, kw)) or {})
    r = client.post('/ping', json={'event': 'ai_referral',
                                   'engine': 'perplexity'})
    assert r.status_code == 200
    assert calls[0][0] == 'ai_referral'
    assert calls[0][1]['engine'] == 'perplexity'


def test_ping_legacy_shape_not_persisted(monkeypatch, app_mod, client):
    calls = []
    monkeypatch.setattr(app_mod.analytics_log, 'log_event',
                        lambda event, **kw: calls.append(event) or {})
    r = client.post('/ping', json={'category': 'camera', 'source_count': 3})
    assert r.status_code == 200
    assert r.get_json() == {'received': True}
    assert calls == []          # legacy pings never reach the store


def test_ping_unknown_event_falls_through_to_legacy(monkeypatch, app_mod,
                                                    client):
    calls = []
    monkeypatch.setattr(app_mod.analytics_log, 'log_event',
                        lambda event, **kw: calls.append(event) or {})
    r = client.post('/ping', json={'event': 'pageview', 'category': 'x'})
    assert r.status_code == 200
    assert calls == []


def test_ping_empty_body_tolerated(app_mod, client):
    r = client.post('/ping', json=None,
                    content_type='application/json')
    assert r.status_code == 200


# ─── /subscribe ─────────────────────────────────────────────────────────────

def test_subscribe_added(monkeypatch, app_mod, client):
    monkeypatch.setattr(app_mod.subscribers, 'add',
                        lambda email, source='site': 'added')
    r = client.post('/subscribe', json={'email': 'a@b.co'})
    assert r.status_code == 200
    assert r.get_json() == {'ok': True}


def test_subscribe_exists_indistinguishable_from_added(monkeypatch, app_mod,
                                                       client):
    monkeypatch.setattr(app_mod.subscribers, 'add',
                        lambda email, source='site': 'exists')
    r = client.post('/subscribe', json={'email': 'a@b.co'})
    assert r.status_code == 200
    assert r.get_json() == {'ok': True}      # no address-book oracle


def test_subscribe_invalid_400(monkeypatch, app_mod, client):
    monkeypatch.setattr(app_mod.subscribers, 'add',
                        lambda email, source='site': 'invalid')
    r = client.post('/subscribe', json={'email': 'nope'})
    assert r.status_code == 400
    assert r.get_json()['ok'] is False


def test_subscribe_honeypot_silent_success_no_write(monkeypatch, app_mod,
                                                    client):
    calls = []
    monkeypatch.setattr(app_mod.subscribers, 'add',
                        lambda email, source='site': calls.append(email))
    r = client.post('/subscribe', json={'email': 'bot@spam.io',
                                        'website': 'http://spam.io'})
    assert r.status_code == 200
    assert r.get_json() == {'ok': True}
    assert calls == []          # nothing written, nothing revealed
