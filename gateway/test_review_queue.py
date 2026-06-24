"""Tests for review_queue.py — the async slug-ambiguity adjudication store.

The invariants under test (these are the reason the module exists):
  - enqueue captures an ambiguous resolution WITHOUT touching skus.json
  - enqueue is idempotent (same product twice -> one record)
  - promote re-runs the collision gate; a colliding override is HARD rejected
  - promote writes the spine via the SAME upsert path the cadre uses
  - promote freezes from the captured identity (no re-fetch needed)
  - reject writes nothing to the spine and demands a structured reason
  - status transitions are one-way (no re-adjudicating a closed record)
  - a promoted slug becomes an override-table fact (resolve_slug now frozen)
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_queue  # noqa: E402
import skus_registry  # noqa: E402
import slug_normalizer  # noqa: E402
from slug_normalizer import SlugResolution  # noqa: E402


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
            'price_seen': {'value': '2498.00', 'currency': 'USD', 'as_of': '2026-06-24'},
        },
        'affiliate_url': 'https://ebay/aff?campid=5339138080',
    }


def _ambiguous_generated():
    """A SlugResolution as the live route would get for a NEW product with no
    authored slug — a generated proposal, needs_review=True."""
    return SlugResolution(
        slug='sony-a7-iv', source='generated', input_text='Sony A7 IV',
        needs_review=True, collision=None,
    )


def _ambiguous_collision():
    return SlugResolution(
        slug='sony-a7-iv', source='generated', input_text='Sony A7 IV',
        needs_review=True, collision='sony-a7iv',
    )


@pytest.fixture
def queue_path(tmp_path):
    return tmp_path / 'review_queue.json'


@pytest.fixture
def skus_path(tmp_path):
    """An empty spine so collision checks have a known baseline."""
    p = tmp_path / 'skus.json'
    p.write_text(json.dumps({
        '_description': 'test', 'version': '0.1.0', 'as_of': '2026-06-24', 'skus': {}
    }))
    return p


# ── enqueue ─────────────────────────────────────────────────────────────────

def test_enqueue_captures_without_touching_spine(queue_path, skus_path):
    rec = review_queue.enqueue(
        _ambiguous_generated(), _resolved(), 'Sony', 'A7 IV', 'body',
        path=queue_path)
    assert rec['status'] == 'pending'
    assert rec['reason'] == 'needs_review'
    assert rec['proposed_slug'] == 'sony-a7-iv'
    assert rec['identity']['epid'] == 'EP123'
    # The spine was never written by an enqueue.
    assert not skus_path.exists() or skus_registry.load_registry(skus_path)['skus'] == {}


def test_enqueue_freezes_identity(queue_path):
    rec = review_queue.enqueue(
        _ambiguous_generated(), _resolved(title='FROZEN TITLE'),
        'Sony', 'A7 IV', 'body', path=queue_path)
    assert rec['identity']['market_title'] == 'FROZEN TITLE'
    assert rec['affiliate_url'].startswith('https://ebay/aff')


def test_enqueue_collision_reason(queue_path):
    rec = review_queue.enqueue(
        _ambiguous_collision(), _resolved(), 'Sony', 'A7 IV', 'body',
        path=queue_path)
    assert rec['reason'] == 'collision'
    assert rec['collision_with'] == 'sony-a7iv'


def test_enqueue_idempotent_same_product(queue_path):
    a = review_queue.enqueue(_ambiguous_generated(), _resolved(), 'Sony', 'A7 IV',
                             'body', path=queue_path)
    b = review_queue.enqueue(_ambiguous_generated(), _resolved(), 'Sony', 'A7 IV',
                             'body', path=queue_path)
    assert a['queue_id'] == b['queue_id']
    assert len(review_queue.load_pending(queue_path)) == 1


def test_enqueue_distinct_products_distinct_records(queue_path):
    review_queue.enqueue(_ambiguous_generated(), _resolved(epid='EP1'),
                         'Sony', 'A7 IV', 'body', path=queue_path)
    review_queue.enqueue(_ambiguous_generated(), _resolved(epid='EP2'),
                         'Canon', 'R6', 'body', path=queue_path)
    assert len(review_queue.load_pending(queue_path)) == 2


def test_contamination_key_defaults_to_proposed_slug(queue_path):
    rec = review_queue.enqueue(_ambiguous_generated(), _resolved(), 'Sony',
                               'A7 IV', 'body', path=queue_path)
    assert rec['contamination_key'] == 'sony-a7-iv'
    rec2 = review_queue.enqueue(_ambiguous_generated(), _resolved(epid='Z'), 'Z',
                                'Z', 'body', contamination_key='custom-key',
                                path=queue_path)
    assert rec2['contamination_key'] == 'custom-key'


# ── promote ─────────────────────────────────────────────────────────────────

def test_promote_writes_spine_via_upsert(queue_path, skus_path):
    rec = review_queue.enqueue(_ambiguous_generated(), _resolved(), 'Sony',
                               'A7 IV', 'body', path=queue_path)
    out, status = review_queue.promote(rec['queue_id'], 'sony-a7iv',
                                       skus_path=skus_path, path=queue_path)
    assert status == 'created'
    assert out['status'] == 'promoted'
    assert out['promoted_as'] == 'sony-a7iv'
    # The spine now carries the entry under the AUTHORIZED slug.
    spine = skus_registry.load_registry(skus_path)['skus']
    assert 'sony-a7iv' in spine
    assert spine['sony-a7iv']['identity']['epid'] == 'EP123'
    assert spine['sony-a7iv']['vendor'] == 'Sony'


def test_promote_uses_frozen_identity_no_refetch(queue_path, skus_path):
    """The spine entry must come from the identity FROZEN at enqueue, proving no
    re-fetch is needed at promote time."""
    rec = review_queue.enqueue(_ambiguous_generated(),
                               _resolved(title='IDENTITY AS OF TAP'),
                               'Sony', 'A7 IV', 'body', path=queue_path)
    review_queue.promote(rec['queue_id'], 'sony-a7iv',
                         skus_path=skus_path, path=queue_path)
    spine = skus_registry.load_registry(skus_path)['skus']
    assert spine['sony-a7iv']['identity']['market_title'] == 'IDENTITY AS OF TAP'


def test_promote_with_colliding_override_is_rejected(queue_path, skus_path):
    """The load-bearing invariant: promotion is NOT a bypass. A human-authorized
    slug that still collides with an existing spine slug is HARD rejected and
    nothing is written."""
    # Seed the spine with an existing slug.
    reg = skus_registry.load_registry(skus_path)
    reg['skus']['sony-a7iv'] = {
        'vendor': 'Sony', 'model': 'A7 IV', 'identity': {'epid': 'OLD'}}
    skus_path.write_text(json.dumps(reg))

    rec = review_queue.enqueue(_ambiguous_generated(), _resolved(), 'Canon',
                               'EOS', 'body', path=queue_path)
    # 'sony-a7-iv' normalizes the same as existing 'sony-a7iv' -> collision.
    with pytest.raises(review_queue.PromotionRejected, match='not a bypass'):
        review_queue.promote(rec['queue_id'], 'sony-a7-iv',
                             skus_path=skus_path, path=queue_path)
    # Record stays pending; spine unchanged (still only the seeded entry).
    assert review_queue.get(rec['queue_id'], queue_path)['status'] == 'pending'
    assert set(skus_registry.load_registry(skus_path)['skus']) == {'sony-a7iv'}


def test_promote_then_resolve_slug_is_frozen_fact(queue_path, skus_path):
    """After promotion the slug is an override-table fact: resolve_slug now
    resolves this vendor/model BY IDENTITY to the frozen slug, source=override,
    needs_review=False — exactly how the cadre behaves."""
    rec = review_queue.enqueue(_ambiguous_generated(), _resolved(), 'Sony',
                               'A7 IV', 'body', path=queue_path)
    review_queue.promote(rec['queue_id'], 'sony-a7iv',
                         skus_path=skus_path, path=queue_path)
    res = slug_normalizer.resolve_slug('Sony', 'A7 IV', skus_path=skus_path)
    assert res.slug == 'sony-a7iv'
    assert res.source == 'override'
    assert res.needs_review is False


def test_promote_idempotent_via_upsert(queue_path, skus_path):
    """Promoting and then (somehow) re-running upsert on the same identity is
    'unchanged' — the spine doesn't fork. We can't re-promote a promoted record
    (one-way status), but the underlying upsert is idempotent."""
    rec = review_queue.enqueue(_ambiguous_generated(), _resolved(), 'Sony',
                               'A7 IV', 'body', path=queue_path)
    review_queue.promote(rec['queue_id'], 'sony-a7iv',
                         skus_path=skus_path, path=queue_path)
    entry = skus_registry.build_entry('sony-a7iv', 'Sony', 'A7 IV', 'body',
                                      'sony-a7-iv', _resolved())
    assert skus_registry.upsert('sony-a7iv', entry, path=skus_path) == 'unchanged'


def test_cannot_promote_already_adjudicated(queue_path, skus_path):
    rec = review_queue.enqueue(_ambiguous_generated(), _resolved(), 'Sony',
                               'A7 IV', 'body', path=queue_path)
    review_queue.promote(rec['queue_id'], 'sony-a7iv',
                         skus_path=skus_path, path=queue_path)
    with pytest.raises(ValueError, match='already adjudicated'):
        review_queue.promote(rec['queue_id'], 'sony-a7iv-2',
                             skus_path=skus_path, path=queue_path)


def test_promote_missing_record_raises(queue_path, skus_path):
    with pytest.raises(KeyError):
        review_queue.promote('deadbeef0000', 'x', skus_path=skus_path,
                             path=queue_path)


# ── reject ──────────────────────────────────────────────────────────────────

def test_reject_writes_nothing_to_spine(queue_path, skus_path):
    rec = review_queue.enqueue(_ambiguous_generated(), _resolved(), 'Sony',
                               'A7 IV', 'body', path=queue_path)
    out = review_queue.reject(rec['queue_id'], 'bad_identity', path=queue_path)
    assert out['status'] == 'rejected'
    assert out['reject_reason'] == 'bad_identity'
    # skus.json untouched (still the empty seed).
    assert skus_registry.load_registry(skus_path)['skus'] == {}


def test_reject_requires_structured_reason(queue_path):
    rec = review_queue.enqueue(_ambiguous_generated(), _resolved(), 'Sony',
                               'A7 IV', 'body', path=queue_path)
    with pytest.raises(ValueError, match='structured'):
        review_queue.reject(rec['queue_id'], 'it looked weird', path=queue_path)


def test_rejected_record_leaves_pending_view(queue_path):
    rec = review_queue.enqueue(_ambiguous_generated(), _resolved(), 'Sony',
                               'A7 IV', 'body', path=queue_path)
    review_queue.reject(rec['queue_id'], 'thin_resolve', path=queue_path)
    assert review_queue.load_pending(queue_path) == []
    # But the record is retained as the adjudication log.
    assert review_queue.get(rec['queue_id'], queue_path)['status'] == 'rejected'


def test_cannot_reject_already_adjudicated(queue_path, skus_path):
    rec = review_queue.enqueue(_ambiguous_generated(), _resolved(), 'Sony',
                               'A7 IV', 'body', path=queue_path)
    review_queue.reject(rec['queue_id'], 'duplicate', path=queue_path)
    with pytest.raises(ValueError):
        review_queue.reject(rec['queue_id'], 'other', path=queue_path)


# ── load helpers ────────────────────────────────────────────────────────────

def test_load_queue_missing_file_is_empty(queue_path):
    assert review_queue.load_queue(queue_path)['queue'] == {}
    assert review_queue.load_pending(queue_path) == []
