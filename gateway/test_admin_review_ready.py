"""Tests for admin_surface.py Piece 4 — the Review Ready PUBLISH gate.

The slug-review surface (test_admin_surface.py) adjudicates ambiguous IDENTITY
into the spine. This file covers the SECOND gate, different in kind: a clean-
built card (work_queue state review_ready) the human renders live (publish) or
declines (reject-card). What these lock down:

  - Auth is mandatory here too — publish RENDERS LIVE, the highest-consequence
    action after a spine write. No ADMIN_TOKEN => 503; wrong/absent => 401.
  - The provenance trace is the robust treatment: a curated entry reads quietly;
    a machine-minted entry gets the loud badge PLUS the trace (source, eBay
    category id); a mint whose category fell back to '' gets the explicit
    "look harder" alert.
  - The option-2 integrity gate: a review_ready card with NO spine entry (or an
    unreadable card.json) is VISIBLE but publish-DISABLED — and the server-side
    /admin/publish route re-asserts that gate, so a stale form can't bypass it.
  - Render-before-state-advance ordering: mark_published runs ONLY after a clean
    render. A render failure leaves the record review_ready (retry-able), never
    promoted-with-no-live-card.
  - reject-card goes through work_queue.reject_card with a CARD_REJECT_REASONS
    code; an unknown reason is a banner, not a 500; nothing is published.
  - The cockpit reads work_queue.counts(); the failed panel reads
    load_by_state('failed') — a build CRASH, distinct from a content reject.
"""
import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import work_queue          # noqa: E402
import skus_registry       # noqa: E402
import admin_surface       # noqa: E402

from flask import Flask    # noqa: E402


TOKEN = 'test-admin-secret'


# ── path-repoint helper (work_queue/skus bind path as a frozen default) ─────

def _repoint(monkeypatch, func, **newdefaults):
    if func.__defaults__:
        names = func.__code__.co_varnames[
            func.__code__.co_argcount - len(func.__defaults__):
            func.__code__.co_argcount]
        new = tuple(newdefaults.get(n, d)
                    for n, d in zip(names, func.__defaults__))
        monkeypatch.setattr(func, '__defaults__', new)
    if func.__kwdefaults__:
        kw = dict(func.__kwdefaults__)
        for n, v in newdefaults.items():
            if n in kw:
                kw[n] = v
        monkeypatch.setattr(func, '__kwdefaults__', kw)


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """Redirect work_queue + skus_registry persistence to temp files, and point
    the admin surface's spine reads at the same temp skus.json."""
    wpath = tmp_path / 'work_queue.json'
    spath = tmp_path / 'skus.json'
    spath.write_text(json.dumps({
        '_description': 'test', 'version': '0.1.0', 'as_of': '2026-06-30',
        'skus': {}}))

    monkeypatch.setattr(work_queue, 'WORK_QUEUE_PATH', wpath)
    monkeypatch.setattr(skus_registry, 'SKUS_PATH', spath)

    for fn in (work_queue.load_queue, work_queue._atomic_write, work_queue.enroll,
               work_queue.claim_next, work_queue.mark_review_ready,
               work_queue.mark_published, work_queue.reject_card,
               work_queue.mark_failed_or_retry,
               work_queue.load_by_state, work_queue.counts, work_queue.get,
               work_queue.cap_remaining):
        _repoint(monkeypatch, fn, path=wpath)
    for fn in (skus_registry.load_registry, skus_registry._atomic_write,
               skus_registry.upsert):
        _repoint(monkeypatch, fn, path=spath)

    return {'work': wpath, 'skus': spath, 'tmp': tmp_path}


class _Render:
    """Injectable fake publish render runner: callable(card_path) -> (rc, detail).

    Records every call so tests assert render-before-state-advance ordering and
    that a render failure leaves state untouched. Default succeeds; flip .rc to
    simulate a build_site failure."""
    def __init__(self, rc=0, detail='ok'):
        self.rc = rc
        self.detail = detail
        self.calls = []

    def __call__(self, card_path):
        self.calls.append(card_path)
        return self.rc, self.detail


@pytest.fixture
def render():
    return _Render()


@pytest.fixture
def client(stores, render, monkeypatch):
    monkeypatch.setenv('ADMIN_TOKEN', TOKEN)
    app = Flask(__name__)
    admin_surface.register_admin(app, render_runner=render)
    app.config['TESTING'] = True
    return app.test_client()


# ── builders ────────────────────────────────────────────────────────────────

def _card_json(stores, slug, **over):
    """Write an assembled card.json under tmp and return its path."""
    card = {
        'card_version': '1.0', 'card_id': slug, 'vertical': 'photography',
        'identity': {
            'display_name': over.get('display_name', 'Sigma 35mm f/1.4 DG DN Art'),
            'brand': 'Sigma', 'model': '35mm f/1.4 DG DN Art',
            'category': 'lens', 'subcategory': 'prime',
            'image_thumb': 'https://img/sigma.png',
        },
        'pricing': {'current_new_usd': over.get('price', 899.0)},
        'freshness': {'source_count': 42, 'build_model': 'claude-test'},
        'confidence': {'overall': 'medium'},
    }
    p = stores['tmp'] / f'card-{slug}.json'
    p.write_text(json.dumps(card))
    return str(p)


def _spine(stores, slug, *, source='resolved', minted_needs_review=False,
           category='lens', ebay_category_id='3323'):
    """Write a spine entry for `slug` directly into the temp skus.json."""
    reg = json.loads(stores['skus'].read_text())
    reg['skus'][slug] = {
        'contamination_key': slug, 'vendor': 'Sigma', 'model': '35 Art',
        'category': category,
        'identity': {'epid': '', 'ebay_category_id': ebay_category_id,
                     'brand': 'Sigma', 'mpn': '304965'},
        'affiliate': {'ebay_epn_url': '', 'amazon_asin': None},
        'source': source, 'minted_needs_review': minted_needs_review,
        'resolved_at': '2026-06-30T00:00:00Z',
    }
    stores['skus'].write_text(json.dumps(reg))


def _enroll_ready(stores, slug, *, card_path=None, with_spine=True, **spine_kw):
    """Drive a slug to review_ready: enroll -> claim -> mark_review_ready, attach
    a card_path, and (optionally) write its spine entry."""
    work_queue.enroll(slug, f'{slug} label', 'lens')
    work_queue.claim_next()
    work_queue.mark_review_ready(slug)
    cp = card_path if card_path is not None else _card_json(stores, slug)
    # attach card_path the way card_factory._attach_card_path does
    q = work_queue.load_queue()
    q['queue'][slug]['card_path'] = cp
    work_queue._atomic_write(q)
    if with_spine:
        _spine(stores, slug, **spine_kw)


def _auth(user='admin', password=TOKEN):
    raw = base64.b64encode(f'{user}:{password}'.encode()).decode()
    return {'Authorization': f'Basic {raw}'}


# ── auth: publish renders live, so the gate is mandatory ────────────────────

def test_publish_routes_fail_closed_without_token(stores, render, monkeypatch):
    monkeypatch.delenv('ADMIN_TOKEN', raising=False)
    app = Flask(__name__)
    admin_surface.register_admin(app, render_runner=render)
    c = app.test_client()
    assert c.post('/admin/publish', headers=_auth()).status_code == 503
    assert c.post('/admin/reject-card', headers=_auth()).status_code == 503


def test_publish_routes_challenge_without_creds(client):
    assert client.post('/admin/publish').status_code == 401
    assert client.post('/admin/reject-card').status_code == 401


def test_publish_routes_reject_wrong_creds(client):
    bad = _auth(password='nope')
    assert client.post('/admin/publish', headers=bad,
                       data={'slug': 's'}).status_code == 401


# ── render: the Review Ready section + cockpit ──────────────────────────────

def test_review_ready_card_renders_with_content(client, stores):
    _enroll_ready(stores, 'sigma-35-art')
    body = client.get('/admin', headers=_auth()).get_data(as_text=True)
    assert 'Review Ready' in body
    assert 'Sigma 35mm f/1.4 DG DN Art' in body   # display_name from card.json
    assert '42 sources' in body                    # freshness.source_count
    assert 'Publish live' in body
    # cockpit reads counts(): one review_ready
    assert 'review ready' in body


def test_empty_review_ready_section(client, stores):
    body = client.get('/admin', headers=_auth()).get_data(as_text=True)
    assert 'nothing review-ready' in body


# ── provenance: the robust trace ────────────────────────────────────────────

def test_curated_entry_reads_quietly(client, stores):
    _enroll_ready(stores, 'sigma-35-art', source='resolved',
                  minted_needs_review=False)
    body = client.get('/admin', headers=_auth()).get_data(as_text=True)
    assert 'curated identity' in body
    assert 'machine-minted' not in body


def test_minted_entry_gets_badge_and_trace(client, stores):
    _enroll_ready(stores, 'sigma-50-art', source='generated',
                  minted_needs_review=True, category='lens',
                  ebay_category_id='3323')
    body = client.get('/admin', headers=_auth()).get_data(as_text=True)
    assert 'machine-minted' in body          # the loud badge
    assert 'source' in body and 'generated' in body
    assert '3323' in body                     # eBay category id in the trace
    assert 'derived from eBay' in body


def test_minted_category_fallback_triggers_look_harder(client, stores):
    # category == '' on a mint => the eBay category id didn't map to vocab.
    _enroll_ready(stores, 'tamron-x', source='generated',
                  minted_needs_review=True, category='',
                  ebay_category_id='99999')
    body = client.get('/admin', headers=_auth()).get_data(as_text=True)
    assert 'category fell back to empty' in body
    assert 'NO' in body  # "this card has NO category"


# ── option-2 integrity gate: visible but publish-disabled ───────────────────

def test_review_ready_without_spine_is_publish_disabled(client, stores):
    _enroll_ready(stores, 'orphan-slug', with_spine=False)
    body = client.get('/admin', headers=_auth()).get_data(as_text=True)
    assert 'orphan-slug' in body                  # visible
    assert 'no spine entry' in body               # reason shown
    assert 'disabled' in body                     # publish button disabled
    assert 'no provenance' in body


def test_review_ready_unreadable_card_is_publish_disabled(client, stores):
    _enroll_ready(stores, 'badcard', card_path='/nonexistent/card.json')
    body = client.get('/admin', headers=_auth()).get_data(as_text=True)
    assert 'badcard' in body
    assert 'missing or unreadable' in body


def test_publish_route_reasserts_integrity_gate(client, stores, render):
    # A forged/stale form posting a slug with no spine entry must be refused
    # server-side even though the page would have disabled the button.
    _enroll_ready(stores, 'orphan-slug', with_spine=False)
    resp = client.post('/admin/publish', headers=_auth(),
                       data={'slug': 'orphan-slug'})
    assert resp.status_code == 200
    assert 'no spine entry' in resp.get_data(as_text=True)
    assert render.calls == []                       # never rendered
    assert work_queue.get('orphan-slug')['state'] == 'review_ready'  # unchanged


# ── publish: render-before-state-advance ────────────────────────────────────

def test_publish_renders_then_promotes(client, stores, render):
    _enroll_ready(stores, 'sigma-35-art')
    resp = client.post('/admin/publish', headers=_auth(),
                       data={'slug': 'sigma-35-art'})
    assert resp.status_code == 200
    assert 'is live' in resp.get_data(as_text=True)
    assert len(render.calls) == 1                    # rendered exactly once
    assert work_queue.get('sigma-35-art')['state'] == 'promoted'


def test_publish_render_failure_leaves_review_ready(client, stores, render):
    render.rc, render.detail = 1, 'build_site exploded'
    _enroll_ready(stores, 'sigma-35-art')
    resp = client.post('/admin/publish', headers=_auth(),
                       data={'slug': 'sigma-35-art'})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Render failed' in body
    assert 'build_site exploded' in body
    # NOT promoted — render failed, state untouched, retry-able
    assert work_queue.get('sigma-35-art')['state'] == 'review_ready'


def test_publish_unknown_slug_banner(client, stores):
    resp = client.post('/admin/publish', headers=_auth(),
                       data={'slug': 'ghost'})
    assert resp.status_code == 200
    assert 'no work-queue record' in resp.get_data(as_text=True)


def test_publish_non_review_ready_banner(client, stores, render):
    # enroll only (state 'resolved'), then try to publish
    work_queue.enroll('justresolved', 'label', 'lens')
    _spine(stores, 'justresolved')
    resp = client.post('/admin/publish', headers=_auth(),
                       data={'slug': 'justresolved'})
    assert resp.status_code == 200
    assert 'not review_ready' in resp.get_data(as_text=True)
    assert render.calls == []


# ── reject-card: content-quality signal, publishes nothing ──────────────────

def test_reject_card_parks_rejected_publishes_nothing(client, stores, render):
    _enroll_ready(stores, 'sigma-35-art')
    resp = client.post('/admin/reject-card', headers=_auth(),
                       data={'slug': 'sigma-35-art', 'reason': 'thin_sources'})
    assert resp.status_code == 200
    assert 'Rejected' in resp.get_data(as_text=True)
    assert work_queue.get('sigma-35-art')['state'] == 'rejected'
    assert render.calls == []                        # nothing published


def test_reject_card_bad_reason_banner(client, stores):
    _enroll_ready(stores, 'sigma-35-art')
    resp = client.post('/admin/reject-card', headers=_auth(),
                       data={'slug': 'sigma-35-art', 'reason': 'because'})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'not in' in body or 'structured' in body
    # untouched — still review_ready
    assert work_queue.get('sigma-35-art')['state'] == 'review_ready'


def test_reject_reasons_rendered_in_dropdown(client, stores):
    _enroll_ready(stores, 'sigma-35-art')
    body = client.get('/admin', headers=_auth()).get_data(as_text=True)
    for reason in work_queue.CARD_REJECT_REASONS:
        assert reason in body


# ── failed panel: a build CRASH, distinct from a content reject ─────────────

def test_failed_panel_shows_crashed_builds(client, stores):
    # Force a slug into `failed`: enroll, claim, exhaust retries.
    work_queue.enroll('brokenbuild', 'label', 'lens', max_attempts=1)
    work_queue.claim_next()
    work_queue.mark_failed_or_retry('brokenbuild', 'fetch stage 500')
    body = client.get('/admin', headers=_auth()).get_data(as_text=True)
    assert 'failed build' in body
    assert 'brokenbuild' in body
    assert 'fetch stage 500' in body
