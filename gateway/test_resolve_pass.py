"""
test_resolve_pass.py — offline tests for the resolve-pass driver.

A FAKE resolve_fn returns each resolver outcome ('resolved', 'queued',
'no_candidate', ResolveError); a tmp skus.json registers the proposal slugs so the
real lookup_proposal (used by the pass to get build identity) works. Proves:
  - only 'resolved' outcomes enroll into the work_queue
  - 'queued' / 'no_candidate' are counted but NOT enrolled (already homed elsewhere)
  - ResolveError on one proposal doesn't abort the batch
  - idempotency: re-running the pass doesn't double-enroll
  - load_proposals normalizes both dict and tuple artifact shapes, sorts by fork_n
  - the enrolled work_queue record carries the right build identity
"""
import json
import pytest

import resolve_sku
import work_queue as wq
import resolve_pass


# ── fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture
def skus_path(tmp_path):
    """Spine pre-seeded with several proposal slugs as existing registry entries."""
    p = tmp_path / 'skus.json'
    p.write_text(json.dumps({
        '_description': 'test', 'version': '0.1.0', 'as_of': '2026-06-29',
        'skus': {
            'sony-a7s-iii': {
                'contamination_key': 'sony-a7s-iii',
                'vendor': 'Sony', 'model': 'A7S III', 'category': 'body',
                'aliases': ['ILCE-7SM3', 'a7siii'],
            },
            'canon-r5-ii': {
                'contamination_key': 'canon-r5-ii',
                'vendor': 'Canon', 'model': 'R5 Mark II', 'category': 'body',
                'aliases': ['EOS R5 II'],
            },
            'pd-travel-tripod': {
                'contamination_key': 'pd-travel-tripod',
                'vendor': 'Peak Design', 'model': 'Travel Tripod', 'category': 'support',
                'aliases': [],
            },
        },
    }))
    return p


@pytest.fixture
def wq_path(tmp_path):
    return tmp_path / 'work_queue.json'


def _fake_resolver(outcomes):
    """Build a resolve_fn that returns a scripted outcome per slug.

    outcomes: {slug: 'resolved'|'queued'|'no_candidate'|'error'}
    'error' raises ResolveError (simulating a slug with no registry entry).
    """
    def resolve_fn(slug, **kwargs):
        kind = outcomes.get(slug, 'resolved')
        if kind == 'error':
            raise resolve_sku.ResolveError(f"{slug} has no registry entry")
        return {'slug': slug, 'outcome': kind, 'detail': 'x', 'confidence': 0.9}
    return resolve_fn


def _props(*slugs_with_forks):
    """[(slug, fork_n), ...] -> normalized proposal dicts."""
    return [{'slug': s, 'fork_n': n} for s, n in slugs_with_forks]


# ── enroll-only-resolved routing ──────────────────────────────────────────────
def test_only_resolved_outcomes_enroll(skus_path, wq_path):
    proposals = _props(('sony-a7s-iii', 13), ('canon-r5-ii', 9), ('pd-travel-tripod', 7))
    resolve_fn = _fake_resolver({
        'sony-a7s-iii': 'resolved',
        'canon-r5-ii': 'queued',          # low-confidence straggler
        'pd-travel-tripod': 'no_candidate',  # unmet want
    })
    summary = resolve_pass.run(
        proposals, ebay=None, gemma=None, demand_log=None, review_queue=None,
        resolve_fn=resolve_fn, skus_path=skus_path, work_queue_path=wq_path)

    assert summary['enrolled'] == 1
    assert summary['enrolled_slugs'] == ['sony-a7s-iii']
    assert summary['already_queued'] == 1
    assert summary['no_candidate'] == 1
    # only the resolved one is in the work queue
    assert wq.get('sony-a7s-iii', path=wq_path) is not None
    assert wq.get('canon-r5-ii', path=wq_path) is None
    assert wq.get('pd-travel-tripod', path=wq_path) is None


def test_enrolled_record_carries_build_identity(skus_path, wq_path):
    proposals = _props(('sony-a7s-iii', 13))
    resolve_fn = _fake_resolver({'sony-a7s-iii': 'resolved'})
    resolve_pass.run(
        proposals, ebay=None, gemma=None, demand_log=None, review_queue=None,
        resolve_fn=resolve_fn, skus_path=skus_path, work_queue_path=wq_path)

    rec = wq.get('sony-a7s-iii', path=wq_path)
    assert rec['state'] == 'resolved'
    assert rec['label'] == 'Sony A7S III'        # vendor + model from registry
    assert rec['category'] == 'body'
    assert rec['aliases'] == ['ILCE-7SM3', 'a7siii']


# ── error tolerance ───────────────────────────────────────────────────────────
def test_resolve_error_does_not_abort_batch(skus_path, wq_path):
    # 'ghost' raises ResolveError; the batch must still process the others.
    proposals = _props(('sony-a7s-iii', 13), ('ghost', 11), ('canon-r5-ii', 8))
    resolve_fn = _fake_resolver({
        'sony-a7s-iii': 'resolved', 'ghost': 'error', 'canon-r5-ii': 'resolved'})
    summary = resolve_pass.run(
        proposals, ebay=None, gemma=None, demand_log=None, review_queue=None,
        resolve_fn=resolve_fn, skus_path=skus_path, work_queue_path=wq_path)

    assert summary['enrolled'] == 2
    assert summary['errors'] == 1
    assert summary['error_slugs'] == ['ghost']
    assert wq.get('sony-a7s-iii', path=wq_path) is not None
    assert wq.get('canon-r5-ii', path=wq_path) is not None


# ── idempotency ───────────────────────────────────────────────────────────────
def test_rerun_does_not_double_enroll(skus_path, wq_path):
    proposals = _props(('sony-a7s-iii', 13))
    resolve_fn = _fake_resolver({'sony-a7s-iii': 'resolved'})
    kw = dict(ebay=None, gemma=None, demand_log=None, review_queue=None,
              resolve_fn=resolve_fn, skus_path=skus_path, work_queue_path=wq_path)

    resolve_pass.run(proposals, **kw)
    # advance the record so a careless re-enroll would be visible as a reset
    wq.claim_next(path=wq_path)                  # -> building
    resolve_pass.run(proposals, **kw)            # second pass

    rec = wq.get('sony-a7s-iii', path=wq_path)
    assert rec['state'] == 'building'            # NOT reset to resolved
    assert wq.counts(path=wq_path)['total'] == 1


# ── load_proposals normalization ──────────────────────────────────────────────
def test_load_proposals_dict_shape(tmp_path):
    p = tmp_path / 'proposals.json'
    p.write_text(json.dumps([
        {'slug': 'a', 'fork_n': 5},
        {'slug': 'b', 'fork_n': 13},
    ]))
    out = resolve_pass.load_proposals(p)
    assert [d['slug'] for d in out] == ['b', 'a']   # sorted by fork_n desc


def test_load_proposals_tuple_shape(tmp_path):
    # proposals() native shape: (fork_n, comp_id, pos_n, abs_n)
    p = tmp_path / 'proposals.json'
    p.write_text(json.dumps([
        [9, 'canon-r5-ii', 2, 0],
        [13, 'sony-a7s-iii', 4, 1],
    ]))
    out = resolve_pass.load_proposals(p)
    assert out[0] == {'slug': 'sony-a7s-iii', 'fork_n': 13}
    assert out[1] == {'slug': 'canon-r5-ii', 'fork_n': 9}


def test_load_proposals_rejects_non_list(tmp_path):
    p = tmp_path / 'proposals.json'
    p.write_text(json.dumps({'slug': 'a'}))
    with pytest.raises(ValueError):
        resolve_pass.load_proposals(p)


def test_load_proposals_rejects_missing_slug(tmp_path):
    p = tmp_path / 'proposals.json'
    p.write_text(json.dumps([{'fork_n': 5}]))
    with pytest.raises(ValueError):
        resolve_pass.load_proposals(p)


# ── on_event callback ─────────────────────────────────────────────────────────
def test_on_event_fires_per_proposal(skus_path, wq_path):
    proposals = _props(('sony-a7s-iii', 13), ('canon-r5-ii', 9))
    resolve_fn = _fake_resolver({'sony-a7s-iii': 'resolved', 'canon-r5-ii': 'queued'})
    events = []
    resolve_pass.run(
        proposals, ebay=None, gemma=None, demand_log=None, review_queue=None,
        resolve_fn=resolve_fn, skus_path=skus_path, work_queue_path=wq_path,
        on_event=events.append)
    assert len(events) == 2
    assert {e['outcome'] for e in events} == {'resolved', 'queued'}


def test_unknown_outcome_counted_as_error(skus_path, wq_path):
    proposals = _props(('sony-a7s-iii', 13))

    def weird_resolver(slug, **kwargs):
        return {'slug': slug, 'outcome': 'banana'}

    summary = resolve_pass.run(
        proposals, ebay=None, gemma=None, demand_log=None, review_queue=None,
        resolve_fn=weird_resolver, skus_path=skus_path, work_queue_path=wq_path)
    assert summary['errors'] == 1
    assert summary['enrolled'] == 0
