"""
test_work_queue.py — offline tests for the card-factory build-lifecycle store.

All tests use a tmp_path work_queue.json (no box, no network). They prove:
  - the happy-path lifecycle resolved -> building -> review_ready -> promoted
  - enroll idempotency / forward-only (never resets progress)
  - the retry budget: non-zero build retries to `resolved`, then parks `failed`
  - built_today increments ONLY on clean build, and rolls over at a new UTC day
  - mark_published advances review_ready -> promoted (the /admin publish gate)
  - reject_card parks a declined clean card in `rejected` (distinct from `failed`)
  - claim_next FIFO ordering and None-when-empty
  - state-guard errors (wrong-state transitions raise)
"""
import json
import time
import pytest

import work_queue as wq


# ── helpers ───────────────────────────────────────────────────────────────────
def _path(tmp_path):
    return tmp_path / 'work_queue.json'


def _enroll_three(p):
    # Stagger enrolled_at so FIFO ordering is deterministic.
    a = wq.enroll('sony-a7iv', 'Sony A7 IV', 'body', path=p)
    time.sleep(0.01)
    b = wq.enroll('sony-a7s-iii', 'Sony A7S III', 'body', path=p)
    time.sleep(0.01)
    c = wq.enroll('pd-travel-tripod', 'Peak Design Travel Tripod', 'support', path=p)
    return a, b, c


# ── enroll ────────────────────────────────────────────────────────────────────
def test_enroll_creates_resolved_record(tmp_path):
    p = _path(tmp_path)
    rec = wq.enroll('sony-a7iv', 'Sony A7 IV', 'body',
                    seed_urls='fixtures/seed-urls/sony-a7iv.json',
                    aliases=['a7iv', 'a7 mark iv'], mount='E', path=p)
    assert rec['state'] == 'resolved'
    assert rec['slug'] == 'sony-a7iv'
    assert rec['label'] == 'Sony A7 IV'
    assert rec['category'] == 'body'
    assert rec['seed_urls'] == 'fixtures/seed-urls/sony-a7iv.json'
    assert rec['aliases'] == ['a7iv', 'a7 mark iv']
    assert rec['mount'] == 'E'
    assert rec['build_attempts'] == 0
    assert rec['max_attempts'] == wq.DEFAULT_MAX_ATTEMPTS


def test_enroll_is_idempotent_forward_only(tmp_path):
    p = _path(tmp_path)
    wq.enroll('sony-a7iv', 'Sony A7 IV', 'body', path=p)
    # advance it past resolved
    wq.claim_next(path=p)  # -> building
    # re-enroll must NOT reset it back to resolved
    again = wq.enroll('sony-a7iv', 'Sony A7 IV', 'body', path=p)
    assert again['state'] == 'building'
    # still exactly one record
    assert wq.counts(path=p)['total'] == 1


# ── claim_next / FIFO ─────────────────────────────────────────────────────────
def test_claim_next_fifo_and_marks_building(tmp_path):
    p = _path(tmp_path)
    _enroll_three(p)
    first = wq.claim_next(path=p)
    assert first['slug'] == 'sony-a7iv'        # oldest enrolled
    assert first['state'] == 'building'
    second = wq.claim_next(path=p)
    assert second['slug'] == 'sony-a7s-iii'    # next oldest


def test_claim_next_none_when_no_resolved(tmp_path):
    p = _path(tmp_path)
    assert wq.claim_next(path=p) is None
    wq.enroll('sony-a7iv', 'Sony A7 IV', 'body', path=p)
    wq.claim_next(path=p)                       # consume the only resolved
    assert wq.claim_next(path=p) is None        # nothing left resolved


# ── happy-path lifecycle ──────────────────────────────────────────────────────
def test_full_lifecycle_to_promoted(tmp_path):
    p = _path(tmp_path)
    wq.enroll('sony-a7iv', 'Sony A7 IV', 'body', path=p)
    wq.claim_next(path=p)                        # building
    wq.mark_review_ready('sony-a7iv', path=p)    # review_ready
    assert wq.get('sony-a7iv', path=p)['state'] == 'review_ready'
    # human published it at /admin (the one in-the-loop gate):
    rec = wq.mark_published('sony-a7iv', path=p)
    assert rec['state'] == 'promoted'
    assert 'published_at' in rec


def test_mark_review_ready_increments_built_today(tmp_path):
    p = _path(tmp_path)
    wq.enroll('sony-a7iv', 'Sony A7 IV', 'body', path=p)
    wq.claim_next(path=p)
    assert wq.counts(path=p)['built_today'] == 0
    wq.mark_review_ready('sony-a7iv', path=p)
    assert wq.counts(path=p)['built_today'] == 1


# ── retry budget ──────────────────────────────────────────────────────────────
def test_failed_build_retries_then_parks_failed(tmp_path):
    p = _path(tmp_path)
    wq.enroll('sony-a7iv', 'Sony A7 IV', 'body', max_attempts=2, path=p)

    # attempt 1: claim -> fail -> back to resolved (retry remains)
    wq.claim_next(path=p)
    rec, terminal = wq.mark_failed_or_retry('sony-a7iv', 'fetch timeout', path=p)
    assert terminal is False
    assert rec['state'] == 'resolved'
    assert rec['build_attempts'] == 1

    # attempt 2: claim -> fail -> budget exhausted -> failed
    wq.claim_next(path=p)
    rec, terminal = wq.mark_failed_or_retry('sony-a7iv', 'fetch timeout again', path=p)
    assert terminal is True
    assert rec['state'] == 'failed'
    assert rec['build_attempts'] == 2
    assert 'fetch timeout again' in rec['last_error']


def test_failed_build_does_not_count_against_cap(tmp_path):
    p = _path(tmp_path)
    wq.enroll('sony-a7iv', 'Sony A7 IV', 'body', max_attempts=1, path=p)
    wq.claim_next(path=p)
    wq.mark_failed_or_retry('sony-a7iv', 'boom', path=p)
    assert wq.counts(path=p)['built_today'] == 0   # only clean builds count


def test_requeue_reopens_failed(tmp_path):
    p = _path(tmp_path)
    wq.enroll('sony-a7iv', 'Sony A7 IV', 'body', max_attempts=1, path=p)
    wq.claim_next(path=p)
    wq.mark_failed_or_retry('sony-a7iv', 'boom', path=p)
    reopened = wq.requeue('sony-a7iv', path=p)
    assert reopened['state'] == 'resolved'
    assert reopened['build_attempts'] == 0
    assert reopened['requeued_from'] == 'failed'
    # and it can be claimed again
    assert wq.claim_next(path=p)['slug'] == 'sony-a7iv'


def test_requeue_reopens_rejected(tmp_path):
    p = _path(tmp_path)
    wq.enroll('sony-a7iv', 'Sony A7 IV', 'body', path=p)
    wq.claim_next(path=p)
    wq.mark_review_ready('sony-a7iv', path=p)
    wq.reject_card('sony-a7iv', 'bad_synthesis', path=p)
    reopened = wq.requeue('sony-a7iv', path=p)
    assert reopened['state'] == 'resolved'
    assert reopened['requeued_from'] == 'rejected'
    assert reopened['reject_reason'] is None


def test_requeue_only_terminal(tmp_path):
    p = _path(tmp_path)
    wq.enroll('sony-a7iv', 'Sony A7 IV', 'body', path=p)
    with pytest.raises(ValueError):
        wq.requeue('sony-a7iv', path=p)   # it's resolved, not terminal


# ── reject_card (human declines a clean build) ────────────────────────────────
def test_reject_card_parks_rejected(tmp_path):
    p = _path(tmp_path)
    wq.enroll('sony-a7iv', 'Sony A7 IV', 'body', path=p)
    wq.claim_next(path=p)
    wq.mark_review_ready('sony-a7iv', path=p)
    rec = wq.reject_card('sony-a7iv', 'thin_sources', path=p)
    assert rec['state'] == 'rejected'
    assert rec['reject_reason'] == 'thin_sources'
    assert 'rejected_at' in rec


def test_reject_card_requires_review_ready(tmp_path):
    p = _path(tmp_path)
    wq.enroll('sony-a7iv', 'Sony A7 IV', 'body', path=p)
    with pytest.raises(ValueError):
        wq.reject_card('sony-a7iv', 'thin_sources', path=p)   # still resolved


def test_reject_card_reason_is_controlled_vocab(tmp_path):
    p = _path(tmp_path)
    wq.enroll('sony-a7iv', 'Sony A7 IV', 'body', path=p)
    wq.claim_next(path=p)
    wq.mark_review_ready('sony-a7iv', path=p)
    with pytest.raises(ValueError):
        wq.reject_card('sony-a7iv', 'i just dont like it', path=p)


def test_reject_distinct_from_failed(tmp_path):
    # A human-rejected clean card and a crashed build are DIFFERENT terminal states
    p = _path(tmp_path)
    wq.enroll('good', 'Good Cam', 'body', max_attempts=1, path=p)
    time.sleep(0.01)
    wq.enroll('crashy', 'Crashy Cam', 'body', max_attempts=1, path=p)

    wq.claim_next(path=p)                          # good -> building
    wq.mark_review_ready('good', path=p)
    wq.reject_card('good', 'bad_synthesis', path=p)

    wq.claim_next(path=p)                          # crashy -> building
    wq.mark_failed_or_retry('crashy', 'exit 2', path=p)

    c = wq.counts(path=p)
    assert c['rejected'] == 1                       # content-quality signal
    assert c['failed'] == 1                         # mechanical-breakdown signal


# ── publish-gate state guards ─────────────────────────────────────────────────
def test_mark_published_requires_review_ready(tmp_path):
    p = _path(tmp_path)
    wq.enroll('sony-a7iv', 'Sony A7 IV', 'body', path=p)
    wq.claim_next(path=p)                          # building, not review_ready
    with pytest.raises(ValueError):
        wq.mark_published('sony-a7iv', path=p)


# ── cap rollover ──────────────────────────────────────────────────────────────
def test_cap_remaining_and_rollover(tmp_path):
    p = _path(tmp_path)
    wq.enroll('sony-a7iv', 'Sony A7 IV', 'body', path=p)
    wq.claim_next(path=p)
    wq.mark_review_ready('sony-a7iv', path=p)    # built_today = 1
    assert wq.cap_remaining(5, path=p) == 4

    # simulate a stale prior day by rewriting cap_date in the file
    data = json.loads(p.read_text())
    data['cap_date'] = '2000-01-01'
    data['built_today'] = 99
    p.write_text(json.dumps(data))

    # cap_remaining should roll the day, zeroing built_today
    assert wq.cap_remaining(5, path=p) == 5
    assert wq.counts(path=p)['built_today'] == 0


# ── state guards ──────────────────────────────────────────────────────────────
def test_mark_review_ready_requires_building(tmp_path):
    p = _path(tmp_path)
    wq.enroll('sony-a7iv', 'Sony A7 IV', 'body', path=p)
    with pytest.raises(ValueError):
        wq.mark_review_ready('sony-a7iv', path=p)   # still resolved


def test_transitions_on_missing_record_raise(tmp_path):
    p = _path(tmp_path)
    with pytest.raises(KeyError):
        wq.mark_review_ready('ghost', path=p)
    with pytest.raises(KeyError):
        wq.mark_failed_or_retry('ghost', 'x', path=p)


# ── counts / informatics ──────────────────────────────────────────────────────
def test_counts_histogram(tmp_path):
    p = _path(tmp_path)
    _enroll_three(p)
    wq.claim_next(path=p)                          # one building
    c = wq.counts(path=p)
    assert c['resolved'] == 2
    assert c['building'] == 1
    assert c['review_ready'] == 0
    assert c['total'] == 3


def test_corrupt_file_raises(tmp_path):
    p = _path(tmp_path)
    p.write_text('{not valid json', encoding='utf-8')
    with pytest.raises(json.JSONDecodeError):
        wq.load_queue(path=p)


# ── set_seed_urls: the sourcing seam (hand-curated today, producer tomorrow) ─

def _seedable_queue(tmp_path):
    path = tmp_path / 'wq.json'
    wq.enroll('sony-a7s-iii', 'Sony A7S III', 'body', path=path)
    return path


def test_set_seed_urls_on_resolved(tmp_path):
    path = _seedable_queue(tmp_path)
    assert wq.set_seed_urls(
        'sony-a7s-iii', '/var/lib/askmaddi-pipeline/seeds/sony-a7s-iii.json',
        path=path) == 'set'
    rec = wq.load_queue(path)['queue']['sony-a7s-iii']
    assert rec['seed_urls'].endswith('sony-a7s-iii.json')
    assert rec['seeds_set_at']
    assert rec['state'] == 'resolved'          # attach does not advance state


def test_set_seed_urls_replaces_and_refuses_past_resolved(tmp_path):
    path = _seedable_queue(tmp_path)
    wq.set_seed_urls('sony-a7s-iii', '/a.json', path=path)
    assert wq.set_seed_urls('sony-a7s-iii', '/b.json',
                                    path=path) == 'set'     # replace ok
    wq.claim_next(path=path)                        # -> building
    assert wq.set_seed_urls('sony-a7s-iii', '/c.json',
                                    path=path) == 'not-resolved'
    rec = wq.load_queue(path)['queue']['sony-a7s-iii']
    assert rec['seed_urls'] == '/b.json'                    # in-flight inputs intact


def test_set_seed_urls_missing_slug(tmp_path):
    path = _seedable_queue(tmp_path)
    assert wq.set_seed_urls('nope', '/x.json',
                                    path=path) == 'missing-slug'


def test_atomic_write_keeps_store_group_rw(tmp_path):
    """Cross-user store contract (2026-07-02 live find): every rewrite must
    leave the file group-readable/writable, or the OTHER pipeline-group user
    (factory<->gateway) goes blind on the next tick. mkstemp's 0600 must not
    leak through os.replace."""
    import os, stat
    path = tmp_path / 'work_queue.json'
    wq.enroll('s1', 'L', 'lens', path=path)          # first write
    wq.claim_next(path=path)                          # a rewrite
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode & 0o060 == 0o060, oct(mode)           # group r+w survive rewrites
