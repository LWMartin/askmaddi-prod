"""Tests for admin_surface.py — the render-for-approval review surface.

What these lock down (the reasons the surface exists / could go wrong):
  - The surface is a SPINE WRITER, so auth is mandatory: no ADMIN_TOKEN => 503
    (fail closed), wrong/absent token => 401, on GET render AND both POSTs.
  - The render shows the FROZEN card preview + the decision-driving metadata
    (reason badge, proposed slug, collision_with) — built from stored record
    fields, escaped.
  - promote/reject go through the REAL review_queue functions (one gate, no
    parallel logic): a promote writes the spine, a reject writes nothing.
  - A human override that STILL collides surfaces as an inline banner, not a
    500 and not a silent bypass — promotion is not a bypass, even from the UI.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_queue          # noqa: E402
import skus_registry         # noqa: E402
import admin_surface         # noqa: E402
from slug_normalizer import SlugResolution  # noqa: E402

from flask import Flask  # noqa: E402


TOKEN = 'test-admin-secret'


# ── fixtures ────────────────────────────────────────────────────────────────

def _resolved(epid='EP123', title='Sony Alpha A7 IV Body'):
    return {
        'identity': {
            'epid': epid,
            'legacy_item_id': 'v1|111|0',
            'ebay_category_id': '625',
            'brand': 'Sony',
            'mpn': 'ILCE-7M4',
            'market_title': title,
            'image': 'https://img/x.jpg',
            'price_seen': {'value': '2498.00', 'currency': 'USD',
                           'as_of': '2026-06-24'},
        },
        'affiliate_url': 'https://ebay/aff?campid=5339138080',
    }


def _ambiguous_generated():
    return SlugResolution(
        slug='sony-a7-iv', source='generated', input_text='Sony A7 IV',
        needs_review=True, collision=None)


def _ambiguous_collision():
    return SlugResolution(
        slug='sony-a7-iv', source='generated', input_text='Sony A7 IV',
        needs_review=True, collision='sony-a7iv')


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """Redirect review_queue + skus_registry persistence to temp files.

    Footgun: every review_queue/skus_registry function binds its path as a
    DEFAULT ARGUMENT (path=REVIEW_QUEUE_PATH) evaluated at import time, so
    monkeypatching the module attribute alone does NOT reach the route, which
    calls these with no explicit path. We patch both the module attribute (for
    code that reads it live, e.g. slug_normalizer's default) AND the frozen
    function defaults (what the no-arg route calls actually use).
    """
    qpath = tmp_path / 'review_queue.json'
    spath = tmp_path / 'skus.json'
    spath.write_text(json.dumps({
        '_description': 'test', 'version': '0.1.0', 'as_of': '2026-06-24',
        'skus': {}}))

    monkeypatch.setattr(review_queue, 'REVIEW_QUEUE_PATH', qpath)
    monkeypatch.setattr(skus_registry, 'SKUS_PATH', spath)

    def _repoint(func, **newdefaults):
        """Rewrite a function's default/kwdefault path args to temp paths."""
        # positional defaults (path=... as the last positional-or-keyword)
        if func.__defaults__:
            names = func.__code__.co_varnames[
                func.__code__.co_argcount - len(func.__defaults__):
                func.__code__.co_argcount]
            new = tuple(newdefaults.get(n, d)
                        for n, d in zip(names, func.__defaults__))
            monkeypatch.setattr(func, '__defaults__', new)
        # keyword-only defaults (the *, skus_path=..., path=... form)
        if func.__kwdefaults__:
            kw = dict(func.__kwdefaults__)
            for n, v in newdefaults.items():
                if n in kw:
                    kw[n] = v
            monkeypatch.setattr(func, '__kwdefaults__', kw)

    for fn in (review_queue.load_queue, review_queue._atomic_write,
               review_queue.enqueue, review_queue.load_pending,
               review_queue.get, review_queue.promote, review_queue.reject):
        _repoint(fn, path=qpath, skus_path=spath)
    for fn in (skus_registry.load_registry, skus_registry._atomic_write,
               skus_registry.upsert):
        _repoint(fn, path=spath)

    return {'queue': qpath, 'skus': spath}


@pytest.fixture
def client(isolated_store, monkeypatch):
    monkeypatch.setenv('ADMIN_TOKEN', TOKEN)
    app = Flask(__name__)
    admin_surface.register_admin(app)
    app.config['TESTING'] = True
    return app.test_client()


def _enqueue_one(collision=False):
    res = _ambiguous_collision() if collision else _ambiguous_generated()
    return review_queue.enqueue(res, _resolved(), 'Sony', 'A7 IV', 'body')


# ── auth: the surface is a spine writer, so it must fail closed ─────────────

def test_no_token_configured_fails_closed(isolated_store, monkeypatch):
    monkeypatch.delenv('ADMIN_TOKEN', raising=False)
    app = Flask(__name__)
    admin_surface.register_admin(app)
    c = app.test_client()
    assert c.get('/admin?token=anything').status_code == 503
    assert c.post('/admin/promote', data={'token': 'x'}).status_code == 503
    assert c.post('/admin/reject', data={'token': 'x'}).status_code == 503


def test_missing_or_wrong_token_unauthorized(client):
    assert client.get('/admin').status_code == 401
    assert client.get('/admin?token=wrong').status_code == 401
    assert client.post('/admin/promote',
                       data={'token': 'wrong', 'queue_id': 'q',
                             'override_slug': 's'}).status_code == 401
    assert client.post('/admin/reject',
                       data={'token': 'wrong', 'queue_id': 'q',
                             'reason': 'duplicate'}).status_code == 401


def test_correct_token_authorized(client):
    assert client.get(f'/admin?token={TOKEN}').status_code == 200


# ── render: the card preview is part of the review ─────────────────────────

def test_empty_queue_renders(client):
    body = client.get(f'/admin?token={TOKEN}').get_data(as_text=True)
    assert 'Queue is empty' in body
    assert '0 pending' in body


def test_render_shows_frozen_card_and_decision_metadata(client):
    _enqueue_one(collision=True)
    body = client.get(f'/admin?token={TOKEN}').get_data(as_text=True)
    # frozen card preview fields
    assert 'Sony Alpha A7 IV Body' in body
    assert 'ILCE-7M4' in body            # mpn
    assert '2498.00' in body             # price_seen.value
    assert 'https://img/x.jpg' in body   # image
    # decision-driving metadata
    assert 'collision' in body
    assert 'sony-a7iv' in body           # collision_with — what it clashed with
    assert 'sony-a7-iv' in body          # proposed slug pre-filled
    # the reject controlled-vocab is rendered as options
    for reason in review_queue.REJECT_REASONS:
        assert reason in body


def test_render_escapes_record_fields(client):
    review_queue.enqueue(
        _ambiguous_generated(),
        _resolved(title='<script>alert(1)</script>'),
        'Sony', 'A7 IV', 'body')
    body = client.get(f'/admin?token={TOKEN}').get_data(as_text=True)
    assert '<script>alert(1)</script>' not in body
    assert '&lt;script&gt;' in body


# ── promote: through the real gate, writes the spine ───────────────────────

def test_promote_writes_spine_and_clears_pending(client, isolated_store):
    rec = _enqueue_one()
    qid = rec['queue_id']
    resp = client.post('/admin/promote',
                       data={'token': TOKEN, 'queue_id': qid,
                             'override_slug': 'sony-a7-iv-body'})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Promoted' in body
    # spine actually written
    spine = json.loads(isolated_store['skus'].read_text())
    assert 'sony-a7-iv-body' in spine['skus']
    # record left pending? no — it's promoted, so the queue render is empty
    assert review_queue.load_pending() == []


def test_promote_colliding_override_is_banner_not_500(client, isolated_store):
    # Seed the spine with the slug the override will collide against.
    review_queue.promote(_enqueue_one()['queue_id'], 'sony-a7iv')
    # New ambiguous record; operator authorizes a slug that normalizes the same.
    rec = review_queue.enqueue(
        _ambiguous_generated(), _resolved(epid='EP999'),
        'Sony', 'A7-IV', 'body')
    resp = client.post('/admin/promote',
                       data={'token': TOKEN, 'queue_id': rec['queue_id'],
                             'override_slug': 'sony-a7-iv'})
    assert resp.status_code == 200          # NOT a 500
    body = resp.get_data(as_text=True)
    assert 'Promotion rejected' in body     # surfaced as a banner
    # nothing written for the rejected promote — record still pending
    assert any(r['queue_id'] == rec['queue_id']
               for r in review_queue.load_pending())


def test_promote_missing_fields_banner(client):
    _enqueue_one()
    resp = client.post('/admin/promote',
                       data={'token': TOKEN, 'queue_id': '', 'override_slug': ''})
    assert resp.status_code == 200
    assert 'required' in resp.get_data(as_text=True)


# ── reject: writes nothing to the spine, demands a structured reason ───────

def test_reject_marks_record_and_writes_no_spine(client, isolated_store):
    rec = _enqueue_one()
    resp = client.post('/admin/reject',
                       data={'token': TOKEN, 'queue_id': rec['queue_id'],
                             'reason': 'not_the_product'})
    assert resp.status_code == 200
    assert 'Rejected' in resp.get_data(as_text=True)
    assert review_queue.load_pending() == []          # no longer pending
    spine = json.loads(isolated_store['skus'].read_text())
    assert spine['skus'] == {}                         # spine untouched


def test_reject_bad_reason_banner(client):
    rec = _enqueue_one()
    resp = client.post('/admin/reject',
                       data={'token': TOKEN, 'queue_id': rec['queue_id'],
                             'reason': 'because-i-said-so'})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'not in' in body or 'structured' in body
    # still pending — a malformed reject changed nothing
    assert any(r['queue_id'] == rec['queue_id']
               for r in review_queue.load_pending())
