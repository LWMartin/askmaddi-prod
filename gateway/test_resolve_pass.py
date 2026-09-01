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
import datetime
import json
import pytest

import resolve_sku
import work_queue as wq
import resolve_pass

UTC = datetime.timezone.utc


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
    # proposals() native shape: (fork_n, comp_id, pos_n, abs_n). Legacy tuple
    # rows carry no identity, so vendor/model normalize to None — meaning the
    # resolver can enrich an existing slug but cannot MINT a missing one from a
    # tuple-shape proposal (it has no vendor/model to mint from).
    p = tmp_path / 'proposals.json'
    p.write_text(json.dumps([
        [9, 'canon-r5-ii', 2, 0],
        [13, 'sony-a7s-iii', 4, 1],
    ]))
    out = resolve_pass.load_proposals(p)
    assert out[0] == {'slug': 'sony-a7s-iii', 'fork_n': 13,
                      'vendor': None, 'model': None,
                      'gtin': None, 'mpn': None, 'product_url': None}
    assert out[1] == {'slug': 'canon-r5-ii', 'fork_n': 9,
                      'vendor': None, 'model': None,
                      'gtin': None, 'mpn': None, 'product_url': None}


def test_load_proposals_identity_shape_carries_vendor_model(tmp_path):
    # The minting-wire identity shape: {slug, fork_n, vendor, model}. vendor and
    # model survive normalization so resolve_proposal can mint a slug that isn't
    # yet a registry entry. fork_n-desc sort still applies.
    p = tmp_path / 'proposals.json'
    p.write_text(json.dumps([
        {'slug': 'canon-r5-ii', 'fork_n': 9, 'vendor': 'Canon', 'model': 'R5 II'},
        {'slug': 'sony-a7s-iii', 'fork_n': 13, 'vendor': 'Sony', 'model': 'A7S III'},
    ]))
    out = resolve_pass.load_proposals(p)
    assert out[0] == {'slug': 'sony-a7s-iii', 'fork_n': 13,
                      'vendor': 'Sony', 'model': 'A7S III',
                      'gtin': None, 'mpn': None, 'product_url': None}
    assert out[1] == {'slug': 'canon-r5-ii', 'fork_n': 9,
                      'vendor': 'Canon', 'model': 'R5 II',
                      'gtin': None, 'mpn': None, 'product_url': None}


def test_load_proposals_carries_feed_identity(tmp_path):
    # GTIN/MPN-first (step 2): a proposal's gtin/mpn/product_url survive
    # normalization so resolve_multisource can join on them downstream.
    p = tmp_path / 'proposals.json'
    p.write_text(json.dumps([
        {'slug': 'nikon-z5-ii', 'fork_n': 5, 'vendor': 'Nikon', 'model': 'Z5 II',
         'gtin': '0018208027958', 'mpn': '1719',
         'product_url': 'https://www.adorama.com/nkz5ii.html'},
    ]))
    out = resolve_pass.load_proposals(p)
    assert out[0]['gtin'] == '0018208027958'
    assert out[0]['mpn'] == '1719'
    assert out[0]['product_url'] == 'https://www.adorama.com/nkz5ii.html'


def test_load_proposals_dict_shape_without_identity_is_none(tmp_path):
    # A legacy dict row {slug, fork_n} with no vendor/model normalizes vendor and
    # model to None — same enrich-only constraint as the tuple shape.
    p = tmp_path / 'proposals.json'
    p.write_text(json.dumps([{'slug': 'sony-a7s-iii', 'fork_n': 13}]))
    out = resolve_pass.load_proposals(p)
    assert out[0] == {'slug': 'sony-a7s-iii', 'fork_n': 13,
                      'vendor': None, 'model': None,
                      'gtin': None, 'mpn': None, 'product_url': None}


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


# ── pacing: --max cap + skip-already-enrolled ──────────────────────────────────
def _counting_resolver(outcomes):
    """A _fake_resolver that records which slugs it was actually CALLED for, so a
    test can prove the expensive resolve was skipped, not merely uncounted."""
    calls = []

    def resolve_fn(slug, **kwargs):
        calls.append(slug)
        kind = outcomes.get(slug, 'resolved')
        if kind == 'error':
            raise resolve_sku.ResolveError(f"{slug} has no registry entry")
        return {'slug': slug, 'outcome': kind, 'detail': 'x', 'confidence': 0.9}
    return resolve_fn, calls


def test_max_new_caps_new_resolves(skus_path, wq_path):
    """--max N resolves at most N NEW proposals and defers the rest — the
    resolver (the eBay+LLM cost) is called exactly N times, not once per file."""
    proposals = _props(('sony-a7s-iii', 13), ('canon-r5-ii', 9),
                       ('pd-travel-tripod', 7))
    resolve_fn, calls = _counting_resolver({})   # all 'resolved'
    summary = resolve_pass.run(
        proposals, ebay=None, gemma=None, demand_log=None, review_queue=None,
        resolve_fn=resolve_fn, skus_path=skus_path, work_queue_path=wq_path,
        max_new=2)

    assert len(calls) == 2, f"resolver called {len(calls)}x despite --max 2"
    assert summary['enrolled'] == 2
    assert summary['deferred'] == 1
    # fork_n-desc order: a7s-iii(13), canon(9) resolve; tripod(7) defers.
    assert set(calls) == {'sony-a7s-iii', 'canon-r5-ii'}
    assert wq.get('pd-travel-tripod', path=wq_path) is None


def test_none_max_is_unlimited_legacy(skus_path, wq_path):
    """max_new=None (the default / one-shot caller) resolves everything —
    the behaviour the daily full run and every existing caller relies on."""
    proposals = _props(('sony-a7s-iii', 13), ('canon-r5-ii', 9),
                       ('pd-travel-tripod', 7))
    resolve_fn, calls = _counting_resolver({})
    summary = resolve_pass.run(
        proposals, ebay=None, gemma=None, demand_log=None, review_queue=None,
        resolve_fn=resolve_fn, skus_path=skus_path, work_queue_path=wq_path)
    assert len(calls) == 3
    assert summary['enrolled'] == 3
    assert summary['deferred'] == 0


def test_already_enrolled_is_skipped_before_resolve(skus_path, wq_path):
    """A slug already in the work_queue is skipped BEFORE the resolver runs —
    no re-hammering the standing backlog with eBay/LLM calls every tick."""
    wq.enroll('sony-a7s-iii', 'Sony A7S III', 'body', path=wq_path)
    proposals = _props(('sony-a7s-iii', 13), ('canon-r5-ii', 9))
    resolve_fn, calls = _counting_resolver({})
    summary = resolve_pass.run(
        proposals, ebay=None, gemma=None, demand_log=None, review_queue=None,
        resolve_fn=resolve_fn, skus_path=skus_path, work_queue_path=wq_path)

    assert calls == ['canon-r5-ii'], "the already-enrolled slug was re-resolved"
    assert summary['skipped_enrolled'] == 1
    assert summary['enrolled'] == 1


def test_skipped_slugs_do_not_consume_the_max_budget(skus_path, wq_path):
    """The cap counts NEW work, not skips: an already-enrolled slug ahead of a
    fresh one must not eat the single --max slot and starve the fresh one."""
    wq.enroll('sony-a7s-iii', 'Sony A7S III', 'body', path=wq_path)
    # a7s-iii(13) is already enrolled -> skipped; canon(9) is the first NEW one.
    proposals = _props(('sony-a7s-iii', 13), ('canon-r5-ii', 9),
                       ('pd-travel-tripod', 7))
    resolve_fn, calls = _counting_resolver({})
    summary = resolve_pass.run(
        proposals, ebay=None, gemma=None, demand_log=None, review_queue=None,
        resolve_fn=resolve_fn, skus_path=skus_path, work_queue_path=wq_path,
        max_new=1)

    assert calls == ['canon-r5-ii'], (
        "the skip consumed the --max budget, starving the fresh proposal")
    assert summary['skipped_enrolled'] == 1
    assert summary['enrolled'] == 1
    assert summary['deferred'] == 1     # tripod


# ── attempt ledger: head-of-line-block cure ───────────────────────────────────
def test_no_candidate_head_is_ledgered_then_skipped_freeing_the_budget(
        skus_path, wq_path, tmp_path):
    """The starvation bug's exact shape: a perennial no_candidate slug sits at the
    head, ahead of a resolvable tail, under a small --max. Run 1 spends the slot on
    the dead head and defers the tail. WITHOUT the ledger this repeats forever (the
    live `ulanzi-f38` stall). WITH it, run 2 skips the cooling head for free and the
    tail finally resolves."""
    ledger = tmp_path / 'attempts.json'
    proposals = _props(('ulanzi-f38', 8), ('canon-r5-ii', 9))
    outcomes = {'ulanzi-f38': 'no_candidate', 'canon-r5-ii': 'resolved'}

    # Run 1: --max 1 -> ulanzi eats the slot (no_candidate, ledgered); canon deferred.
    rf1, calls1 = _counting_resolver(outcomes)
    s1 = resolve_pass.run(
        proposals, ebay=None, gemma=None, demand_log=None, review_queue=None,
        resolve_fn=rf1, skus_path=skus_path, work_queue_path=wq_path,
        max_new=1, attempts_ledger_path=str(ledger))
    assert calls1 == ['ulanzi-f38']
    assert s1['no_candidate'] == 1 and s1['deferred'] == 1 and s1['enrolled'] == 0
    assert json.loads(ledger.read_text()).get('ulanzi-f38')  # recorded

    # Run 2: same --max 1 -> ulanzi cools off (skipped, no slot); canon resolves.
    rf2, calls2 = _counting_resolver(outcomes)
    s2 = resolve_pass.run(
        proposals, ebay=None, gemma=None, demand_log=None, review_queue=None,
        resolve_fn=rf2, skus_path=skus_path, work_queue_path=wq_path,
        max_new=1, attempts_ledger_path=str(ledger))
    assert calls2 == ['canon-r5-ii'], "the dead head re-blocked the budget; tail starved"
    assert s2['skipped_cooldown'] == 1
    assert s2['enrolled'] == 1
    assert wq.get('canon-r5-ii', path=wq_path) is not None


def test_ledger_entry_expires_after_ttl_allowing_retry(skus_path, wq_path, tmp_path):
    """A cooled slug is skipped within the TTL but retried once it lapses — a
    no_candidate can become resolvable when eBay inventory later appears."""
    ledger = tmp_path / 'attempts.json'
    proposals = _props(('ulanzi-f38', 8))
    t0 = datetime.datetime(2026, 8, 1, tzinfo=UTC)

    rf1, calls1 = _counting_resolver({'ulanzi-f38': 'no_candidate'})
    resolve_pass.run(
        proposals, ebay=None, gemma=None, demand_log=None, review_queue=None,
        resolve_fn=rf1, skus_path=skus_path, work_queue_path=wq_path,
        attempts_ledger_path=str(ledger), retry_ttl_days=7, now=t0)
    assert calls1 == ['ulanzi-f38']

    # +3 days: still within TTL -> skipped, resolver not called.
    rf2, calls2 = _counting_resolver({'ulanzi-f38': 'no_candidate'})
    s2 = resolve_pass.run(
        proposals, ebay=None, gemma=None, demand_log=None, review_queue=None,
        resolve_fn=rf2, skus_path=skus_path, work_queue_path=wq_path,
        attempts_ledger_path=str(ledger), retry_ttl_days=7,
        now=t0 + datetime.timedelta(days=3))
    assert calls2 == [] and s2['skipped_cooldown'] == 1

    # +8 days: past TTL -> retried.
    rf3, calls3 = _counting_resolver({'ulanzi-f38': 'no_candidate'})
    s3 = resolve_pass.run(
        proposals, ebay=None, gemma=None, demand_log=None, review_queue=None,
        resolve_fn=rf3, skus_path=skus_path, work_queue_path=wq_path,
        attempts_ledger_path=str(ledger), retry_ttl_days=7,
        now=t0 + datetime.timedelta(days=8))
    assert calls3 == ['ulanzi-f38'] and s3['no_candidate'] == 1


def test_resolved_outcome_graduates_slug_out_of_ledger(skus_path, wq_path, tmp_path):
    """A slug that was ledgered (past-TTL, so retried) and now resolves must be
    dropped from the ledger — never spuriously cooled after a success."""
    ledger = tmp_path / 'attempts.json'
    ledger.write_text(json.dumps({'canon-r5-ii': '2026-08-01T00:00:00Z'}))
    proposals = _props(('canon-r5-ii', 9))

    rf, calls = _counting_resolver({'canon-r5-ii': 'resolved'})
    resolve_pass.run(
        proposals, ebay=None, gemma=None, demand_log=None, review_queue=None,
        resolve_fn=rf, skus_path=skus_path, work_queue_path=wq_path,
        attempts_ledger_path=str(ledger), retry_ttl_days=7,
        now=datetime.datetime(2026, 8, 20, tzinfo=UTC))
    assert calls == ['canon-r5-ii']
    assert 'canon-r5-ii' not in json.loads(ledger.read_text())


def _renaming_resolver(rename_map, outcomes=None):
    """A resolver that returns a canonical slug DIFFERENT from the proposal slug
    (simulating a mint/rename, e.g. `sony-a7s-iii-original` -> `sony-a7s-iii`).
    Records the slugs it was actually CALLED for so a cheap cooldown skip is
    provable."""
    calls = []

    def resolve_fn(slug, **kwargs):
        calls.append(slug)
        return {'slug': rename_map.get(slug, slug),
                'outcome': (outcomes or {}).get(slug, 'resolved'),
                'detail': 'x', 'confidence': 0.9}
    return resolve_fn, calls


def test_resolved_slug_already_queued_is_decontaminated_permanently(
        skus_path, wq_path, tmp_path):
    """Canonical-slug re-block: a proposal whose RESOLVED slug is already in the
    queue (a rename of an already-built card) slips past the pre-resolve skip (which
    tests the PROPOSAL slug). WITHOUT the guard it counts as a fresh `enrolled`, is
    popped from the ledger, and re-blocks the paced head every tick — the exact
    `sony-a7r-original`/`sigma-35-f12-dg-dn` stall that left `resolved` empty. WITH
    it: NOT enrolled, escorted to the decontamination zone (permanent sentinel, not
    a TTL cool), so the next tick frees the budget for the tail AND it never
    re-blocks — even years later, unlike a transient no_candidate."""
    ledger = tmp_path / 'attempts.json'
    t0 = datetime.datetime(2026, 8, 1, tzinfo=UTC)
    # Canonical already built + promoted (Lee's published card).
    wq.enroll('sony-a7s-iii', 'Sony A7S III', 'body', path=wq_path)
    proposals = _props(('sony-a7s-iii-original', 8), ('canon-r5-ii', 9))
    rename = {'sony-a7s-iii-original': 'sony-a7s-iii'}   # resolves to the built card

    # Run 1: --max 1 -> the rename eats the slot, resolves to an already-queued
    # canonical: NOT enrolled, escorted to decontam by the PROPOSAL slug; canon deferred.
    rf1, calls1 = _renaming_resolver(rename)
    s1 = resolve_pass.run(
        proposals, ebay=None, gemma=None, demand_log=None, review_queue=None,
        resolve_fn=rf1, skus_path=skus_path, work_queue_path=wq_path,
        max_new=1, attempts_ledger_path=str(ledger), now=t0)
    assert calls1 == ['sony-a7s-iii-original']
    assert s1['enrolled'] == 0 and s1['decontaminated'] == 1 and s1['deferred'] == 1
    led = json.loads(ledger.read_text())
    assert led.get('sony-a7s-iii-original') == resolve_pass.DECONTAM_MARK  # permanent
    assert 'sony-a7s-iii' not in led                                       # canonical untouched

    # Run 2 (same day): the rename is skipped for free; the tail finally resolves.
    rf2, calls2 = _renaming_resolver(rename)
    s2 = resolve_pass.run(
        proposals, ebay=None, gemma=None, demand_log=None, review_queue=None,
        resolve_fn=rf2, skus_path=skus_path, work_queue_path=wq_path,
        max_new=1, attempts_ledger_path=str(ledger), now=t0)
    assert calls2 == ['canon-r5-ii'], "the renamed dup re-blocked the budget; tail starved"
    assert s2['skipped_cooldown'] == 1 and s2['enrolled'] == 1
    assert wq.get('canon-r5-ii', path=wq_path) is not None

    # Run 3 (+400 days): decontam is PERMANENT — far past any TTL, still skipped,
    # resolver never called for the escorted slug (contrast the no_candidate TTL test).
    rf3, calls3 = _renaming_resolver(rename)
    resolve_pass.run(
        _props(('sony-a7s-iii-original', 8)),
        ebay=None, gemma=None, demand_log=None, review_queue=None,
        resolve_fn=rf3, skus_path=skus_path, work_queue_path=wq_path,
        max_new=1, attempts_ledger_path=str(ledger), retry_ttl_days=7,
        now=t0 + datetime.timedelta(days=400))
    assert calls3 == [], "decontaminated slug came back after TTL — escort was not permanent"


def test_no_ledger_path_preserves_legacy_retry_every_run(skus_path, wq_path):
    """attempts_ledger_path=None (default / one-shot / test callers) keeps the exact
    legacy behaviour: no cooldown skip, retry every run, nothing persisted."""
    proposals = _props(('ulanzi-f38', 8))
    rf1, calls1 = _counting_resolver({'ulanzi-f38': 'no_candidate'})
    s1 = resolve_pass.run(
        proposals, ebay=None, gemma=None, demand_log=None, review_queue=None,
        resolve_fn=rf1, skus_path=skus_path, work_queue_path=wq_path,
        attempts_ledger_path=None)
    rf2, calls2 = _counting_resolver({'ulanzi-f38': 'no_candidate'})
    s2 = resolve_pass.run(
        proposals, ebay=None, gemma=None, demand_log=None, review_queue=None,
        resolve_fn=rf2, skus_path=skus_path, work_queue_path=wq_path,
        attempts_ledger_path=None)
    assert calls1 == ['ulanzi-f38'] and calls2 == ['ulanzi-f38']  # re-attempted both runs
    assert s1['skipped_cooldown'] == 0 and s2['skipped_cooldown'] == 0


# ── duplicate_identity outcome -> permanent DECONTAM escort ───────────────────
def test_duplicate_identity_counts_and_permanently_escorts(skus_path, wq_path, tmp_path):
    """A cross-slug product-identity dup (resolve_proposal drops it, no mint) is
    counted, NOT enrolled, and its proposal slug is written to the ledger with the
    permanent DECONTAM mark — it re-mints a fresh title-slug each tick, so a TTL
    cool would let it resurrect; the permanent escort keeps it off the paced budget."""
    ledger = tmp_path / 'attempts.json'

    def resolve_fn(slug, **kwargs):
        return {'slug': slug, 'outcome': 'duplicate_identity',
                'dup_of': 'built-canonical'}

    summary = resolve_pass.run(
        _props(('autel-evo-2-pro-rugged', 3)), ebay=None, gemma=None,
        demand_log=None, review_queue=None, resolve_fn=resolve_fn,
        skus_path=skus_path, work_queue_path=wq_path,
        attempts_ledger_path=str(ledger))

    assert summary['duplicate_identity'] == 1
    assert summary['enrolled'] == 0
    assert wq.get('autel-evo-2-pro-rugged', path=wq_path) is None
    saved = json.loads(ledger.read_text())
    assert saved['autel-evo-2-pro-rugged'] == resolve_pass.DECONTAM_MARK
