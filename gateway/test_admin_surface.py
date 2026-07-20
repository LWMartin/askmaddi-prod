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
               skus_registry.upsert, skus_registry.set_gtin,
               skus_registry.adjudicate_gtin):
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


def _auth(user='admin', password=TOKEN):
    """HTTP Basic Authorization header for the test client."""
    import base64
    raw = base64.b64encode(f'{user}:{password}'.encode()).decode()
    return {'Authorization': f'Basic {raw}'}


# ── auth: the surface is a spine writer, so it must fail closed ─────────────

def test_no_token_configured_fails_closed(isolated_store, monkeypatch):
    monkeypatch.delenv('ADMIN_TOKEN', raising=False)
    app = Flask(__name__)
    admin_surface.register_admin(app)
    c = app.test_client()
    # Even WITH valid-looking credentials, an unconfigured surface serves 503.
    assert c.get('/admin', headers=_auth()).status_code == 503
    assert c.post('/admin/promote', headers=_auth()).status_code == 503
    assert c.post('/admin/reject', headers=_auth()).status_code == 503


def test_missing_credentials_challenges(client):
    # No Authorization header → 401 with a Basic challenge so the browser prompts.
    r = client.get('/admin')
    assert r.status_code == 401
    assert r.headers.get('WWW-Authenticate', '').startswith('Basic')


def test_wrong_credentials_unauthorized(client):
    assert client.get('/admin', headers=_auth(password='wrong')).status_code == 401
    assert client.get('/admin', headers=_auth(user='root')).status_code == 401
    assert client.post('/admin/promote', headers=_auth(password='wrong'),
                       data={'queue_id': 'q', 'override_slug': 's'}
                       ).status_code == 401
    assert client.post('/admin/reject', headers=_auth(password='wrong'),
                       data={'queue_id': 'q', 'reason': 'duplicate'}
                       ).status_code == 401


def test_correct_credentials_authorized(client):
    assert client.get('/admin', headers=_auth()).status_code == 200


# ── render: the card preview is part of the review ─────────────────────────

def test_empty_queue_renders(client):
    body = client.get('/admin', headers=_auth()).get_data(as_text=True)
    assert 'Queue is empty' in body
    assert '0 pending' in body


def test_render_shows_frozen_card_and_decision_metadata(client):
    _enqueue_one(collision=True)
    body = client.get('/admin', headers=_auth()).get_data(as_text=True)
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
    body = client.get('/admin', headers=_auth()).get_data(as_text=True)
    assert '<script>alert(1)</script>' not in body
    assert '&lt;script&gt;' in body


# ── promote: through the real gate, writes the spine ───────────────────────

def test_promote_writes_spine_and_clears_pending(client, isolated_store):
    rec = _enqueue_one()
    qid = rec['queue_id']
    resp = client.post('/admin/promote', headers=_auth(),
                       data={'queue_id': qid,
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
    resp = client.post('/admin/promote', headers=_auth(),
                       data={'queue_id': rec['queue_id'],
                             'override_slug': 'sony-a7-iv'})
    assert resp.status_code == 200          # NOT a 500
    body = resp.get_data(as_text=True)
    assert 'Promotion rejected' in body     # surfaced as a banner
    # nothing written for the rejected promote — record still pending
    assert any(r['queue_id'] == rec['queue_id']
               for r in review_queue.load_pending())


def test_promote_missing_fields_banner(client):
    _enqueue_one()
    resp = client.post('/admin/promote', headers=_auth(),
                       data={'queue_id': '', 'override_slug': ''})
    assert resp.status_code == 200
    assert 'required' in resp.get_data(as_text=True)


# ── reject: writes nothing to the spine, demands a structured reason ───────

def test_reject_marks_record_and_writes_no_spine(client, isolated_store):
    rec = _enqueue_one()
    resp = client.post('/admin/reject', headers=_auth(),
                       data={'queue_id': rec['queue_id'],
                             'reason': 'not_the_product'})
    assert resp.status_code == 200
    assert 'Rejected' in resp.get_data(as_text=True)
    assert review_queue.load_pending() == []          # no longer pending
    spine = json.loads(isolated_store['skus'].read_text())
    assert spine['skus'] == {}                         # spine untouched


def test_reject_bad_reason_banner(client):
    rec = _enqueue_one()
    resp = client.post('/admin/reject', headers=_auth(),
                       data={'queue_id': rec['queue_id'],
                             'reason': 'because-i-said-so'})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'not in' in body or 'structured' in body
    # still pending — a malformed reject changed nothing
    assert any(r['queue_id'] == rec['queue_id']
               for r in review_queue.load_pending())


# ── GTIN conflict receipts: read-only Axis A abstain rendering ──────────────
#
# Fixtures modeled on the two live receipt families the first sweep persisted:
# an L1-internal conflict (own listing's observations disagree — the regional
# EAN/UPC case) and an L2 CONFLICT_DROP (admission gate saw >=2 catalog
# candidates on distinct GTINs — the Peak Design variant case).

def _l1_conflict_prov():
    return {
        'chosen_source': 'product.gtins',
        'conflict': True,
        'observations': [
            {'source': 'product.gtins', 'identifier_type': 'EAN',
             'raw': '4548736128338', 'gtin14': '04548736128338',
             'valid': True},
            {'source': 'aspect', 'identifier_type': 'UPC',
             'raw': '027242923355', 'gtin14': '00027242923355',
             'valid': True},
        ],
    }


def _l2_conflict_prov():
    return {
        'chosen_source': None,
        'conflict': True,
        'observations': [
            {'source': 'secondpass:candidate', 'identifier_type': 'GTIN',
             'raw': '00850004509715', 'gtin14': '00850004509715',
             'valid': True, 'item_id': 'v1|111|0', 'epid': 'EP1'},
            {'source': 'secondpass:candidate', 'identifier_type': 'GTIN',
             'raw': '00850004509722', 'gtin14': '00850004509722',
             'valid': True, 'item_id': 'v1|222|0', 'epid': 'EP2'},
        ],
        'recovery': {
            'method': 'ebay-secondpass',
            'query': 'Peak Design TT-CB-5-150-1',
            'verdict': 'CONFLICT_DROP',
            'recovered_at': '2026-07-01T18:00:00Z',
            'n_candidates': 3,
            'n_gtin_bearing': 2,
            'distinct_gtins': ['00850004509715', '00850004509722'],
            'model_token': 'TT-CB-5-150-1',
            'candidates': [
                {'item_id': 'v1|111|0', 'epid': 'EP1',
                 'gtin': '00850004509715', 'chosen_source': 'product.gtins',
                 'title': 'Peak Design Travel Tripod Carbon',
                 'token_match': True},
                {'item_id': 'v1|222|0', 'epid': 'EP2',
                 'gtin': '00850004509722', 'chosen_source': 'product.gtins',
                 'title': 'Peak Design Travel Tripod Aluminum',
                 'token_match': True},
                {'item_id': 'v1|333|0', 'epid': '',
                 'gtin': None, 'chosen_source': None,
                 'title': 'tripod bag only', 'token_match': False,
                 'error': 'resolve timeout'},
            ],
        },
    }


def _spine_with(isolated_store, slug, identity):
    spine = json.loads(isolated_store['skus'].read_text())
    spine['skus'][slug] = {'identity': identity}
    isolated_store['skus'].write_text(json.dumps(spine))


def test_no_conflicts_no_section(client):
    body = client.get('/admin', headers=_auth()).get_data(as_text=True)
    assert 'GTIN Conflicts' not in body


def test_l1_conflict_renders_observations(client, isolated_store):
    _spine_with(isolated_store, 'canon-r5',
                {'vendor': 'canon', 'model': 'R5', 'gtin': None,
                 'gtin_provenance': _l1_conflict_prov()})
    body = client.get('/admin', headers=_auth()).get_data(as_text=True)
    assert 'GTIN Conflicts' in body
    assert 'canon-r5' in body
    assert 'L1 own-listing' in body
    assert '04548736128338' in body and '00027242923355' in body
    assert 'product.gtins' in body and 'aspect' in body
    assert '2 distinct GTINs' in body


def test_l2_conflict_renders_gate_receipt(client, isolated_store):
    _spine_with(isolated_store, 'peak-design-travel-tripod',
                {'vendor': 'peak-design', 'model': 'travel-tripod',
                 'gtin': None, 'gtin_provenance': _l2_conflict_prov()})
    body = client.get('/admin', headers=_auth()).get_data(as_text=True)
    assert 'L2 second-pass' in body
    assert 'CONFLICT_DROP' in body
    assert 'Peak Design TT-CB-5-150-1' in body        # the query
    assert '00850004509715' in body and '00850004509722' in body
    assert 'resolve timeout' in body                   # errored candidate kept
    assert 'TT-CB-5-150-1' in body                     # model token shown


def test_adjudicated_entry_drops_out(client, isolated_store):
    # set_gtin's upgrade-only write IS the resolution: gtin present -> no render
    prov = _l2_conflict_prov()
    _spine_with(isolated_store, 'peak-design-travel-tripod',
                {'vendor': 'peak-design', 'model': 'travel-tripod',
                 'gtin': '00850004509715', 'gtin_provenance': prov})
    body = client.get('/admin', headers=_auth()).get_data(as_text=True)
    assert 'GTIN Conflicts' not in body


def test_non_conflict_provenance_not_rendered(client, isolated_store):
    prov = _l1_conflict_prov()
    prov['conflict'] = False
    _spine_with(isolated_store, 'sony-a7iv',
                {'vendor': 'sony', 'model': 'a7iv', 'gtin': None,
                 'gtin_provenance': prov})
    body = client.get('/admin', headers=_auth()).get_data(as_text=True)
    assert 'GTIN Conflicts' not in body


def test_conflict_receipt_fields_escaped(client, isolated_store):
    prov = _l1_conflict_prov()
    prov['observations'][0]['raw'] = '<script>alert(1)</script>'
    _spine_with(isolated_store, 'hostile-sku',
                {'vendor': '<b>v</b>', 'model': 'm', 'gtin': None,
                 'gtin_provenance': prov})
    body = client.get('/admin', headers=_auth()).get_data(as_text=True)
    assert '<script>alert(1)</script>' not in body
    assert '&lt;script&gt;' in body
    assert '<b>v</b>' not in body


def test_malformed_provenance_skipped_not_500(client, isolated_store):
    _spine_with(isolated_store, 'broken-a',
                {'vendor': 'x', 'model': 'y', 'gtin': None,
                 'gtin_provenance': 'not-a-dict'})
    _spine_with(isolated_store, 'broken-b',
                {'vendor': 'x', 'model': 'y', 'gtin': None,
                 'gtin_provenance': {'conflict': True}})  # no observations
    resp = client.get('/admin', headers=_auth())
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # non-dict skipped entirely; empty-evidence conflict still renders a card
    assert 'broken-a' not in body
    assert 'broken-b' in body


# ── /admin/gtin-resolve: adjudication route (thin boundary over the writer) ──

def test_gtin_resolve_requires_auth(client, isolated_store):
    resp = client.post('/admin/gtin-resolve',
                       data={'slug': 'x', 'action': 'assign', 'gtin': 'g'})
    assert resp.status_code == 401


def test_conflict_card_offers_evidenced_assigns_and_dismiss(client,
                                                            isolated_store):
    _spine_with(isolated_store, 'peak-design-travel-tripod',
                {'vendor': 'peak-design', 'model': 'travel-tripod',
                 'gtin': None, 'gtin_provenance': _l2_conflict_prov()})
    body = client.get('/admin', headers=_auth()).get_data(as_text=True)
    assert '/admin/gtin-resolve' in body
    assert body.count('name="action" value="assign"') == 2   # one per GTIN
    assert 'value="00850004509715"' in body
    assert 'value="00850004509722"' in body
    assert 'name="action" value="dismiss"' in body
    assert 'variant_ambiguous' in body                        # reason vocab


def test_gtin_resolve_assign_roundtrip(client, isolated_store):
    _spine_with(isolated_store, 'peak-design-travel-tripod',
                {'vendor': 'peak-design', 'model': 'travel-tripod',
                 'gtin': None, 'gtin_provenance': _l2_conflict_prov()})
    resp = client.post('/admin/gtin-resolve', headers=_auth(),
                       data={'slug': 'peak-design-travel-tripod',
                             'action': 'assign',
                             'gtin': '00850004509715'})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Assigned 00850004509715' in body
    assert 'GTIN Conflicts' not in body            # resolved -> gone from page
    entry = json.loads(isolated_store['skus'].read_text())[
        'skus']['peak-design-travel-tripod']
    assert entry['gtin'] == '00850004509715'          # top-level Axis A anchor
    prov = entry['identity']['gtin_provenance']       # receipt stays evidence-side
    assert prov['recovery']['verdict'] == 'CONFLICT_DROP'   # receipt intact
    assert prov['adjudications'][0]['action'] == 'assign'


def test_gtin_resolve_dismiss_roundtrip(client, isolated_store):
    _spine_with(isolated_store, 'peak-design-travel-tripod',
                {'vendor': 'peak-design', 'model': 'travel-tripod',
                 'gtin': None, 'gtin_provenance': _l2_conflict_prov()})
    resp = client.post('/admin/gtin-resolve', headers=_auth(),
                       data={'slug': 'peak-design-travel-tripod',
                             'action': 'dismiss',
                             'reason': 'variant_ambiguous'})
    body = resp.get_data(as_text=True)
    assert 'Dismissed' in body
    assert 'GTIN Conflicts' not in body            # dismissed -> gone from page
    idn = json.loads(isolated_store['skus'].read_text())[
        'skus']['peak-design-travel-tripod']['identity']
    assert idn['gtin'] is None
    assert idn['gtin_provenance']['adjudications'][0][
        'reason'] == 'variant_ambiguous'


def test_gtin_resolve_unevidenced_gtin_banner_no_write(client, isolated_store):
    _spine_with(isolated_store, 'peak-design-travel-tripod',
                {'vendor': 'peak-design', 'model': 'travel-tripod',
                 'gtin': None, 'gtin_provenance': _l2_conflict_prov()})
    resp = client.post('/admin/gtin-resolve', headers=_auth(),
                       data={'slug': 'peak-design-travel-tripod',
                             'action': 'assign',
                             'gtin': '00099999999999'})
    body = resp.get_data(as_text=True)
    assert 'gtin-not-evidenced' in body
    assert 'GTIN Conflicts' in body                # still pending on the page
    idn = json.loads(isolated_store['skus'].read_text())[
        'skus']['peak-design-travel-tripod']['identity']
    assert idn['gtin'] is None


def test_gtin_resolve_bad_inputs_banner_not_500(client, isolated_store):
    for data in ({'slug': '', 'action': 'assign'},
                 {'slug': 'nope', 'action': 'assign', 'gtin': 'g'},
                 {'slug': 'nope', 'action': 'frobnicate'}):
        resp = client.post('/admin/gtin-resolve', headers=_auth(), data=data)
        assert resp.status_code == 200


# ── build_site_runner: publish joins the corpus (found live 2026-07-03) ─────

def test_publish_runner_admits_card_and_rebuilds_from_corpus(tmp_path):
    """The gate's first live publish shrank the homepage to one card:
    --manifest regenerates from only the cards loaded that run. Publish must
    ADMIT the card into data/cards/ and rebuild from the whole corpus."""
    cards_dir = tmp_path / 'cards'
    cards_dir.mkdir()
    (cards_dir / 'existing-card.json').write_text(json.dumps(
        {'card_id': 'existing-card'}))
    new_card = tmp_path / 'spool' / 'card.json'
    new_card.parent.mkdir()
    new_card.write_text(json.dumps({'card_id': 'sony-a7s-iii', 'tier': 't'}))

    captured = {}
    fake_site = tmp_path / 'build_site.py'
    fake_site.write_text('')

    import admin_surface as a
    real_run = a.subprocess.run
    def fake_run(cmd, **kw):
        captured['cmd'] = cmd
        class R: returncode, stderr, stdout = 0, '', ''
        return R()
    a.subprocess.run = fake_run
    try:
        runner = a.build_site_runner(build_site_path=fake_site,
                                     output_dir=tmp_path / 'browser',
                                     cards_dir=cards_dir,
                                     indexnow_path=None)
        rc, detail = runner(str(new_card))
    finally:
        a.subprocess.run = real_run

    assert rc == 0
    # admitted into the corpus, existing member untouched
    assert (cards_dir / 'sony-a7s-iii.json').exists()
    assert (cards_dir / 'existing-card.json').exists()
    # rebuild is corpus-wide, never single-card
    assert '--cards-dir' in captured['cmd'] and '--card' not in captured['cmd']
    assert '--manifest' in captured['cmd'] and '--sitemap' in captured['cmd']


def test_publish_runner_republish_is_idempotent_overwrite(tmp_path):
    cards_dir = tmp_path / 'cards'; cards_dir.mkdir()
    src = tmp_path / 'card.json'
    src.write_text(json.dumps({'card_id': 'x', 'v': 1}))
    import admin_surface as a
    real_run = a.subprocess.run
    a.subprocess.run = lambda cmd, **kw: type('R', (), {
        'returncode': 0, 'stderr': '', 'stdout': ''})()
    try:
        runner = a.build_site_runner(build_site_path=tmp_path / 'b.py',
                                     output_dir=tmp_path / 'o',
                                     cards_dir=cards_dir,
                                     indexnow_path=None)
        runner(str(src))
        src.write_text(json.dumps({'card_id': 'x', 'v': 2}))
        rc, _ = runner(str(src))
    finally:
        a.subprocess.run = real_run
    assert rc == 0
    assert json.loads((cards_dir / 'x.json').read_text())['v'] == 2


def test_publish_runner_fails_closed_before_touching_corpus(tmp_path):
    cards_dir = tmp_path / 'cards'; cards_dir.mkdir()
    bad = tmp_path / 'card.json'
    bad.write_text(json.dumps({'no_card_id': True}))
    import admin_surface as a
    runner = a.build_site_runner(build_site_path=tmp_path / 'b.py',
                                 output_dir=tmp_path / 'o',
                                 cards_dir=cards_dir)
    rc, detail = runner(str(bad))
    assert rc == 1 and 'card_id' in detail
    assert list(cards_dir.iterdir()) == []          # corpus untouched


# ── build_site_runner: indexnow hook (2026-07-18, distribution Next #3) ──────

def _capture_runner(tmp_path, ping_rc=0, ping_stdout='indexnow: HTTP 202 — 14'
                    ' url(s) submitted', ping_raises=False):
    """Runner wired to a fake subprocess that records every call and answers
    the render leg with success, the ping leg per the parameters."""
    import admin_surface as a
    cards_dir = tmp_path / 'cards'; cards_dir.mkdir(exist_ok=True)
    card = tmp_path / 'card.json'
    card.write_text(json.dumps({'card_id': 'x'}))
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        if ping_raises and len(calls) > 1:
            raise OSError('boom')
        rc = 0 if len(calls) == 1 else ping_rc
        out = '' if len(calls) == 1 else ping_stdout
        return type('R', (), {'returncode': rc, 'stderr': '', 'stdout': out})()
    return a, card, calls, fake_run, cards_dir


def test_publish_runner_pings_indexnow_after_render(tmp_path):
    a, card, calls, fake_run, cards_dir = _capture_runner(tmp_path)
    real = a.subprocess.run; a.subprocess.run = fake_run
    try:
        runner = a.build_site_runner(build_site_path=tmp_path / 'b.py',
                                     output_dir=tmp_path / 'browser',
                                     cards_dir=cards_dir,
                                     indexnow_path=tmp_path / 'ping.py',
                                     bank_bot_push_path=None)
        rc, detail = runner(str(card))
    finally:
        a.subprocess.run = real
    assert rc == 0 and detail == 'ok (+indexnow)'
    assert len(calls) == 2
    # --browser-dir is passed ABSOLUTELY (the tool's relative default would
    # resolve against the gateway CWD, not the repo)
    ping_cmd = calls[1]
    i = ping_cmd.index('--browser-dir')
    assert ping_cmd[i + 1] == str(tmp_path / 'browser')


def test_publish_runner_indexnow_soft_fail_never_gates_publish(tmp_path):
    a, card, calls, fake_run, cards_dir = _capture_runner(
        tmp_path, ping_rc=1, ping_stdout='indexnow: soft-fail — timeout')
    real = a.subprocess.run; a.subprocess.run = fake_run
    try:
        runner = a.build_site_runner(build_site_path=tmp_path / 'b.py',
                                     output_dir=tmp_path / 'o',
                                     cards_dir=cards_dir,
                                     indexnow_path=tmp_path / 'ping.py',
                                     bank_bot_push_path=None)
        rc, detail = runner(str(card))
    finally:
        a.subprocess.run = real
    assert rc == 0                       # the card is live regardless
    assert detail.startswith('ok (indexnow:')


def test_publish_runner_indexnow_exception_never_gates_publish(tmp_path):
    a, card, calls, fake_run, cards_dir = _capture_runner(
        tmp_path, ping_raises=True)
    real = a.subprocess.run; a.subprocess.run = fake_run
    try:
        runner = a.build_site_runner(build_site_path=tmp_path / 'b.py',
                                     output_dir=tmp_path / 'o',
                                     cards_dir=cards_dir,
                                     indexnow_path=tmp_path / 'ping.py',
                                     bank_bot_push_path=None)
        rc, detail = runner(str(card))
    finally:
        a.subprocess.run = real
    assert rc == 0 and detail.startswith('ok (indexnow:')


def test_publish_runner_no_ping_when_render_fails(tmp_path):
    import admin_surface as a
    cards_dir = tmp_path / 'cards'; cards_dir.mkdir()
    card = tmp_path / 'card.json'
    card.write_text(json.dumps({'card_id': 'x'}))
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        return type('R', (), {'returncode': 2, 'stderr': 'render exploded',
                              'stdout': ''})()
    real = a.subprocess.run; a.subprocess.run = fake_run
    try:
        runner = a.build_site_runner(build_site_path=tmp_path / 'b.py',
                                     output_dir=tmp_path / 'o',
                                     cards_dir=cards_dir,
                                     indexnow_path=tmp_path / 'ping.py',
                                     bank_bot_push_path=None)
        rc, detail = runner(str(card))
    finally:
        a.subprocess.run = real
    assert rc == 2 and len(calls) == 1   # no ping on a dead render

# ── build_site_runner: bank hook (2026-07-20 wire — a7c tree-dirt class) ─────

def _bank_runner(tmp_path, bank_rc=0, bank_stderr='', bank_raises=False,
                 snapshot=True):
    """Runner with indexnow disabled and the bank leg wired to a fake
    subprocess. Fake answers render (call 1) with success and the bank
    (call 2) per the parameters."""
    import admin_surface as a
    cards_dir = tmp_path / 'cards'; cards_dir.mkdir(exist_ok=True)
    card = tmp_path / 'card.json'
    card.write_text(json.dumps({'card_id': 'x'}))
    snap = tmp_path / 'writeback.json'
    if snapshot:
        snap.write_text(json.dumps({'role': 'writeback', 'allowlist': [],
                                    'policies': {}, 'crucible_hash': 'h'}))
    calls = []
    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        if bank_raises and len(calls) > 1:
            raise OSError('door jammed')
        rc = 0 if len(calls) == 1 else bank_rc
        err = '' if len(calls) == 1 else bank_stderr
        return type('R', (), {'returncode': rc, 'stderr': err, 'stdout': ''})()
    return a, card, calls, fake_run, cards_dir, snap


def test_publish_runner_banks_after_render(tmp_path):
    a, card, calls, fake_run, cards_dir, snap = _bank_runner(tmp_path)
    real = a.subprocess.run; a.subprocess.run = fake_run
    try:
        runner = a.build_site_runner(build_site_path=tmp_path / 'b.py',
                                     output_dir=tmp_path / 'o',
                                     cards_dir=cards_dir,
                                     indexnow_path=None,
                                     bank_bot_push_path=tmp_path / 'bp.py',
                                     bank_snapshot=snap,
                                     bank_repo=tmp_path)
        rc, detail = runner(str(card))
    finally:
        a.subprocess.run = real
    assert rc == 0 and detail == 'ok (+bank)'
    assert len(calls) == 2
    bank_cmd = calls[1][0]
    # the door is invoked with the frozen snapshot, the admin_publish job,
    # and a card-scoped summary — audit trail starts at the invocation
    assert '--job' in bank_cmd and \
        bank_cmd[bank_cmd.index('--job') + 1] == 'admin_publish'
    assert bank_cmd[bank_cmd.index('--snapshot') + 1] == str(snap)
    assert 'admin publish: x' in bank_cmd[bank_cmd.index('--summary') + 1]
    assert calls[1][1].get('timeout') == 600


def test_publish_runner_bank_fail_never_gates_publish(tmp_path):
    a, card, calls, fake_run, cards_dir, snap = _bank_runner(
        tmp_path, bank_rc=2, bank_stderr='[bot:admin_publish] BLOCKED — gate failed')
    real = a.subprocess.run; a.subprocess.run = fake_run
    try:
        runner = a.build_site_runner(build_site_path=tmp_path / 'b.py',
                                     output_dir=tmp_path / 'o',
                                     cards_dir=cards_dir,
                                     indexnow_path=None,
                                     bank_bot_push_path=tmp_path / 'bp.py',
                                     bank_snapshot=snap,
                                     bank_repo=tmp_path)
        rc, detail = runner(str(card))
    finally:
        a.subprocess.run = real
    assert rc == 0                        # the card is live regardless
    assert detail.startswith('ok (bank:') and 'BLOCKED' in detail


def test_publish_runner_bank_exception_never_gates_publish(tmp_path):
    a, card, calls, fake_run, cards_dir, snap = _bank_runner(
        tmp_path, bank_raises=True)
    real = a.subprocess.run; a.subprocess.run = fake_run
    try:
        runner = a.build_site_runner(build_site_path=tmp_path / 'b.py',
                                     output_dir=tmp_path / 'o',
                                     cards_dir=cards_dir,
                                     indexnow_path=None,
                                     bank_bot_push_path=tmp_path / 'bp.py',
                                     bank_snapshot=snap,
                                     bank_repo=tmp_path)
        rc, detail = runner(str(card))
    finally:
        a.subprocess.run = real
    assert rc == 0 and detail.startswith('ok (bank:')


def test_publish_runner_missing_snapshot_is_loud_skip(tmp_path):
    a, card, calls, fake_run, cards_dir, snap = _bank_runner(
        tmp_path, snapshot=False)
    real = a.subprocess.run; a.subprocess.run = fake_run
    try:
        runner = a.build_site_runner(build_site_path=tmp_path / 'b.py',
                                     output_dir=tmp_path / 'o',
                                     cards_dir=cards_dir,
                                     indexnow_path=None,
                                     bank_bot_push_path=tmp_path / 'bp.py',
                                     bank_snapshot=snap,
                                     bank_repo=tmp_path)
        rc, detail = runner(str(card))
    finally:
        a.subprocess.run = real
    assert rc == 0
    assert len(calls) == 1               # door never invoked without its snapshot
    assert 'bank: skipped' in detail and str(snap) in detail


def test_publish_runner_no_bank_when_render_fails(tmp_path):
    import admin_surface as a
    cards_dir = tmp_path / 'cards'; cards_dir.mkdir()
    card = tmp_path / 'card.json'
    card.write_text(json.dumps({'card_id': 'x'}))
    snap = tmp_path / 'writeback.json'; snap.write_text('{}')
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        return type('R', (), {'returncode': 2, 'stderr': 'render exploded',
                              'stdout': ''})()
    real = a.subprocess.run; a.subprocess.run = fake_run
    try:
        runner = a.build_site_runner(build_site_path=tmp_path / 'b.py',
                                     output_dir=tmp_path / 'o',
                                     cards_dir=cards_dir,
                                     indexnow_path=None,
                                     bank_bot_push_path=tmp_path / 'bp.py',
                                     bank_snapshot=snap,
                                     bank_repo=tmp_path)
        rc, detail = runner(str(card))
    finally:
        a.subprocess.run = real
    assert rc == 2 and len(calls) == 1   # a dead render banks nothing
