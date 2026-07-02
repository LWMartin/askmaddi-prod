"""
review_queue.py — async adjudication store for slug-ambiguous resolved products.
================================================================================
Demand-factory primitive (maddi-skus-registry, review-queue decision LOCKED
2026-06-24). The structural substitute for "Lee is sitting there to say yes":
the live card-build path can keep its human-review checkpoint AND still create
cards from demand, because ambiguous slugs land HERE — outside the spine — for
asynchronous adjudication, then get promoted into skus.json by the same
frozen-fact mechanism the seed cadre uses.

Where this sits in the gate chain (all live code, grounded):
    user taps unmet product
      -> ebay_api.resolve()                  # lossless identity
      -> slug_normalizer.resolve_slug(...)   # SlugResolution(needs_review, collision)
      -> if ambiguous: enqueue() HERE        # NOT skus.json — outside the spine
         else:         build_entry -> upsert # straight through (facts, not guesses)

The queue is the SUBSET of unmet demand that (a) resolved to a real product and
(b) tripped the slug gate. Broader unmet demand (resolver failures, out-of-catalog
taps) is logged separately by demand_log.py, upstream and independent of this.

Two invariants this module exists to hold:

  1. NOTHING ambiguous enters the spine. A queued record is OUTSIDE skus.json by
     construction (separate file). There is no quarantine flag inside skus.json
     for a downstream reader to forget to check — the 2026-06-23 Sigma
     silent-failure class is structurally impossible here.

  2. Promotion is NEVER a bypass. promote() re-runs the SAME collision gate with
     the human's chosen slug as the override. A human slug that STILL collides is
     STILL hard-rejected — mirroring backfill's rule that an operator --override
     which clashes is a hard reject. The only thing a human supplies is the ONE
     irreducibly-human field (the slug); they do not get to wave a malformed
     identity into the spine.

Records are append-only and status-tracked (pending -> promoted|rejected). A
record is NEVER deleted: the queue doubles as the adjudication log (what was
promoted, what was rejected and why). The reject reason is structured because a
reject is a BUG REPORT against the pipeline — it tells you which generation stage
produced something wrong, not "this card needed touching up" (the review surface
is a judgment surface, never an editing surface; if a card looks wrong the CODE
is wrong, not the card).
"""
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

import skus_registry
import slug_normalizer

REVIEW_QUEUE_PATH = Path(__file__).parent.parent / 'data' / 'review_queue.json'
SCHEMA_VERSION = '0.1.0'

# Structured ENQUEUE reasons — why a resolved product landed in the queue instead
# of flowing straight to the spine. The first two are slug-gate verdicts (the live
# user-tap path: slug_normalizer.resolve_slug tripped). The third is a RESOLVE-side
# verdict from the demand factory: the slug resolved cleanly, but the eBay-candidate
# disambiguation (which item_id IS this product) was below the confidence floor, so
# a human must pick the right listing from the competing candidates. Same queue, same
# /admin cockpit, same promote/reject gate — a different REASON the human is here.
#
#   collision              slug normalizes the same as an existing spine slug
#   needs_review           slug is a generated proposal (ambiguous normalization)
#   low_resolve_confidence factory's Gemma pick was uncertain — see `candidates`
ENQUEUE_REASONS = ('collision', 'needs_review', 'low_resolve_confidence')

# Structured reject reasons — a reject is a pipeline-bug signal, so the reason is
# a controlled vocab the upstream-fix workflow can key on, not freetext.
REJECT_REASONS = (
    'bad_identity',      # resolver returned a garbled / wrong identity
    'wrong_price',       # price_seen is implausible / $0 / stale-wrong
    'thin_resolve',      # identity came back too sparse to build a card
    'not_the_product',   # resolved to a different item than the user wanted
    'duplicate',         # already covered by an existing spine card
    'other',             # escape hatch — should be rare; prefer a specific code
)


class PromotionRejected(Exception):
    """A promote() failed the collision gate — the human's chosen slug still
    clashes with an existing spine slug under normalization. HARD reject; nothing
    written. Mirrors backfill.SlugRejected: a human override that collides is
    still a hard reject, because promotion is not a bypass of the spine's
    purity invariant.
    """


def _empty_queue():
    return {
        '_description': (
            'Async review queue for slug-ambiguous resolved products. OUTSIDE '
            'the skus.json spine by construction. Append-only, status-tracked; '
            'records are never deleted (adjudication log).'
        ),
        'version': SCHEMA_VERSION,
        'as_of': time.strftime('%Y-%m-%d', time.gmtime()),
        'queue': {},
    }


def _queue_id(vendor, model, identity):
    """Idempotent dedup key: the same unmet product tapped twice -> one record.

    Keyed on vendor|model|epid. epid is the stable machine identity; when it's
    absent we fall back to vendor|model alone so a null-epid resolve still
    dedups on its human identity rather than minting a fresh record per tap.
    """
    epid = (identity or {}).get('epid', '') or ''
    basis = f"{vendor}|{model}|{epid}".lower()
    return hashlib.sha1(basis.encode('utf-8')).hexdigest()[:12]


def load_queue(path=REVIEW_QUEUE_PATH):
    """Return the queue dict, or a fresh empty one if absent.

    Tolerant of a missing file (first run), not of a corrupt one — a malformed
    review_queue.json is a real error the caller should see, not silently
    overwrite. Same discipline as skus_registry.load_registry.
    """
    path = Path(path)
    if not path.exists():
        return _empty_queue()
    return json.loads(path.read_text(encoding='utf-8'))


def _atomic_write(queue, path=REVIEW_QUEUE_PATH):
    """Atomic whole-file write (temp in same dir + os.replace).

    The queue is a mutable map (status transitions rewrite a record), so unlike
    demand_log's append-only stream it uses the same temp+replace dance as
    skus_registry — a reader never observes a half-written queue.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.rq-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(queue, fh, indent=2, ensure_ascii=False)
            fh.write('\n')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def enqueue(resolution, resolved, vendor, model, category,
            contamination_key=None, path=REVIEW_QUEUE_PATH,
            *, reason_override=None, candidates=None):
    """Capture one slug-ambiguous resolved product for async review.

    Called by the live route exactly where backfill's _gate_slug would raise
    SlugRejected — but instead of discarding the proposal (correct for an
    offline human-present run), the live path persists it here.

    Parameters
    ----------
    resolution : slug_normalizer.SlugResolution
        The ambiguous resolution (needs_review=True or collision set). Its
        reason determines the record's `reason` field.
    resolved : dict
        The ebay_api.resolve() result — {'identity': {...}, 'affiliate_url': ...}.
        Its identity block is FROZEN into the record so promotion is a pure local
        write with no second eBay round-trip.
    vendor, model, category : str
        The product's human/controlled-vocab identity.
    contamination_key : str | None
        Best-guess editorial bridge key; correctable at promote time. Defaults to
        the proposed slug when not supplied (a starting point, not a commitment).
    reason_override : str | None
        Force the record's `reason` (must be in ENQUEUE_REASONS). The live user-tap
        path leaves this None and the reason is derived from the slug gate
        (collision vs needs_review). The demand FACTORY passes
        'low_resolve_confidence' — its slug resolved cleanly, but the eBay-candidate
        pick was uncertain, so the human is here to choose the listing, not the slug.
    candidates : list[dict] | None
        The ranked competing eBay candidates the factory's disambiguator weighed,
        attached ONLY for a low_resolve_confidence enqueue. Each is the human's
        "did the machine overlook the right product?" surface (the resolve-time
        analog of the extract-time near-miss sidecar). Shape per candidate:
        {item_id, title, price, currency, condition, score, chosen}. Frozen into
        the record so /admin renders them with no eBay re-fetch.

    Idempotent: same product (vendor|model|epid) twice -> the existing pending
    record is returned untouched, no duplicate. A record already promoted/rejected
    is also returned as-is (re-tapping an adjudicated product does not reopen it
    here; that's a demand_log event, logged upstream).

    Returns the record dict.
    """
    identity = dict((resolved or {}).get('identity', {}))
    qid = _queue_id(vendor, model, identity)

    queue = load_queue(path)
    q = queue.setdefault('queue', {})

    existing = q.get(qid)
    if existing is not None:
        # Already seen — pending or already adjudicated. Idempotent: don't fork,
        # don't reopen. Return what's there.
        return existing

    if reason_override is not None:
        if reason_override not in ENQUEUE_REASONS:
            raise ValueError(
                f"reason_override {reason_override!r} not in {ENQUEUE_REASONS} — "
                f"the enqueue reason is a controlled vocab, not freetext."
            )
        reason = reason_override
    else:
        reason = 'collision' if resolution.collision else 'needs_review'
    record = {
        'queue_id': qid,
        'reason': reason,
        'proposed_slug': resolution.slug,
        'collision_with': resolution.collision,
        'input_text': resolution.input_text,
        'vendor': vendor,
        'model': model,
        'category': category,
        'contamination_key': contamination_key or resolution.slug,
        'identity': identity,
        'affiliate_url': (resolved or {}).get('affiliate_url', ''),
        'enqueued_at': _now(),
        'status': 'pending',
        'reject_reason': None,
    }
    if candidates:
        # Frozen ranked candidates — the "was a valid product overlooked?" surface.
        # Only present for low_resolve_confidence; absent for slug-gate enqueues.
        record['candidates'] = candidates
    q[qid] = record
    queue['as_of'] = time.strftime('%Y-%m-%d', time.gmtime())
    _atomic_write(queue, path)
    return record


def load_pending(path=REVIEW_QUEUE_PATH):
    """Return the list of pending records (the review surface's read model).

    Only status=='pending'; promoted/rejected records stay in the file as the
    adjudication log but are not part of the work-to-do view.
    """
    queue = load_queue(path)
    return [r for r in queue.get('queue', {}).values()
            if r.get('status') == 'pending']


def get(queue_id, path=REVIEW_QUEUE_PATH):
    """Fetch one record by id (None if absent)."""
    return load_queue(path).get('queue', {}).get(queue_id)


def promote(queue_id, override_slug, *, skus_path=skus_registry.SKUS_PATH,
            path=REVIEW_QUEUE_PATH):
    """Adjudicate a pending record INTO the spine, under the slug Lee authorizes.

    This is the inverse of enqueue and it reuses the EXACT spine-write path the
    cadre uses:

      1. override_slug is the human's authoritative slug — the ONE sanctioned
         judgment field. It becomes a new override-table fact.
      2. Re-run the SAME collision gate (slug_normalizer.resolve_slug with
         override=override_slug) against the live spine. A chosen slug that STILL
         collides -> PromotionRejected, nothing written. Promotion is not a bypass.
      3. On pass -> skus_registry.build_entry() from the FROZEN queue identity ->
         skus_registry.upsert(). Idempotent + atomic, identical to backfill.
      4. Mark the record status=promoted (never deleted — adjudication log).

    After promotion the promoted slug is a frozen override-table entry in
    skus.json; from then on slug_normalizer.resolve_slug resolves this
    vendor/model BY IDENTITY to the frozen slug — exactly how the cadre behaves.

    Returns (record, upsert_status) where upsert_status is
    'created'|'updated'|'unchanged' from skus_registry.upsert.

    Raises KeyError if the record is absent, ValueError if it isn't pending,
    PromotionRejected if the authorized slug collides.
    """
    queue = load_queue(path)
    q = queue.get('queue', {})
    record = q.get(queue_id)
    if record is None:
        raise KeyError(f"no review-queue record {queue_id!r}")
    if record.get('status') != 'pending':
        raise ValueError(
            f"record {queue_id!r} is {record.get('status')!r}, not pending — "
            f"already adjudicated, refusing to re-promote."
        )

    vendor, model = record['vendor'], record['model']

    # Re-run the gate with the human's chosen slug as the override. Same function
    # the request handler / backfill use — one definition of "does this slug clash
    # with the spine." A colliding human override is a hard reject.
    res = slug_normalizer.resolve_slug(
        vendor, model, override=override_slug, skus_path=skus_path)
    if res.collision:
        raise PromotionRejected(
            f"authorized slug '{override_slug}' normalizes the same as existing "
            f"spine slug '{res.collision}' (sony-a7iv ~ sony-a7-iv class). "
            f"Promotion is not a bypass — resolve the clash and re-promote with a "
            f"non-colliding slug."
        )

    # Build the spine entry from the FROZEN identity captured at enqueue. No
    # re-fetch — the identity is a fact as of the tap.
    entry = skus_registry.build_entry(
        slug=override_slug,
        vendor=vendor,
        model=model,
        facet=record['category'],
        contamination_key=record['contamination_key'],
        resolved={
            'identity': record['identity'],
            'affiliate_url': record.get('affiliate_url', ''),
        },
    )
    status = skus_registry.upsert(override_slug, entry, path=skus_path)

    # Record the outcome. promoted_as captures the slug Lee chose (may differ from
    # the original proposed_slug — that's the whole point of override).
    record['status'] = 'promoted'
    record['promoted_as'] = override_slug
    record['promoted_at'] = _now()
    queue['as_of'] = time.strftime('%Y-%m-%d', time.gmtime())
    _atomic_write(queue, path)
    return record, status


def reresolve(queue_id, chosen_item_id, *, ebay, path=REVIEW_QUEUE_PATH):
    """Re-freeze a low_resolve_confidence record onto a DIFFERENT candidate.

    The correction loop for "the machine picked the wrong eBay listing." When the
    demand factory's disambiguator was uncertain (reason=low_resolve_confidence)
    and the human, looking at the visible `candidates`, sees a different listing is
    the real product, this swaps the record's frozen identity to that listing —
    then the human promotes through the SAME slug-gated promote() path as always.

    The loop is sanctioned; the abuse is fenced out by construction:

      1. SCOPED — only a low_resolve_confidence record can be re-resolved. A
         collision/needs_review record is a SLUG decision, not a listing decision;
         re-resolving it is meaningless and refused.
      2. CLOSED SET — chosen_item_id MUST be one of THIS record's own candidates.
         The human picks among what the factory actually weighed; they cannot
         inject an arbitrary item_id. (Overlooked != unconstrained.)
      3. REAL RESOLVE — the new identity comes from a genuine ebay.resolve() round
         trip, never hand-supplied fields. The human chooses WHICH listing; they
         never author WHAT the identity says. The review surface stays a judgment
         surface, never an editing surface.
      4. STILL PENDING — re-resolve does NOT promote. It re-freezes identity and
         re-marks the chosen candidate, leaving the record pending so the normal
         collision-gated promote() is still the only door into the spine.

    Parameters
    ----------
    queue_id : str
    chosen_item_id : str
        An item_id drawn from record['candidates']. Anything else -> ValueError.
    ebay : module/obj
        Injected eBay resolver exposing .resolve(item_id) -> {'identity', ...}.
        Injected (not imported) so this is unit-testable offline with a mock, the
        same discipline resolve_sku uses.

    Returns the updated record (still status=pending).

    Raises KeyError if absent, ValueError if not pending, not low_resolve_confidence,
    or chosen_item_id is not among the record's candidates.
    """
    queue = load_queue(path)
    q = queue.get('queue', {})
    record = q.get(queue_id)
    if record is None:
        raise KeyError(f"no review-queue record {queue_id!r}")
    if record.get('status') != 'pending':
        raise ValueError(
            f"record {queue_id!r} is {record.get('status')!r}, not pending — "
            f"cannot re-resolve an already-adjudicated record."
        )
    if record.get('reason') != 'low_resolve_confidence':
        raise ValueError(
            f"record {queue_id!r} reason is {record.get('reason')!r}; re-resolve "
            f"only applies to a low_resolve_confidence record (a listing decision). "
            f"A collision/needs_review record is a SLUG decision — fix it at promote."
        )

    candidates = record.get('candidates') or []
    cand_ids = {c.get('item_id') for c in candidates}
    if chosen_item_id not in cand_ids:
        raise ValueError(
            f"item_id {chosen_item_id!r} is not among this record's candidates — "
            f"re-resolve is constrained to the listings the factory actually "
            f"weighed; an arbitrary item_id is refused (overlooked, not unbounded)."
        )

    # Real eBay round-trip for the chosen listing — identity is a fact from the
    # marketplace, never hand-edited.
    resolved = ebay.resolve(chosen_item_id)
    record['identity'] = dict((resolved or {}).get('identity', {}))
    record['affiliate_url'] = (resolved or {}).get('affiliate_url', '')

    # Re-mark which candidate is chosen so the /admin render and any later audit
    # reflect the human's correction; confidence on the human-chosen row is 1.0
    # (a human decision, not a model score).
    for c in candidates:
        is_chosen = (c.get('item_id') == chosen_item_id)
        c['chosen'] = is_chosen
        if is_chosen:
            c['score'] = 1.0
            c['human_chosen'] = True
    record['reresolved_at'] = _now()
    queue['as_of'] = time.strftime('%Y-%m-%d', time.gmtime())
    _atomic_write(queue, path)
    return record


def reject(queue_id, reason, *, path=REVIEW_QUEUE_PATH):
    """Reject a pending record. Writes NOTHING to the spine.

    `reason` must be one of REJECT_REASONS — a reject is a structured BUG REPORT
    against the pipeline (which generation stage produced something wrong), not a
    freetext note and not "this card needed editing." The review surface is a
    judgment surface: the correct response to a card that looks wrong is reject +
    route the defect upstream to the code, never a card-level patch.

    Marks status=rejected (record retained as adjudication log). Returns the
    record. Raises KeyError if absent, ValueError if not pending or if reason is
    not a recognized code.
    """
    if reason not in REJECT_REASONS:
        raise ValueError(
            f"reject reason {reason!r} not in {REJECT_REASONS} — a reject is a "
            f"structured pipeline-bug signal, not freetext."
        )
    queue = load_queue(path)
    q = queue.get('queue', {})
    record = q.get(queue_id)
    if record is None:
        raise KeyError(f"no review-queue record {queue_id!r}")
    if record.get('status') != 'pending':
        raise ValueError(
            f"record {queue_id!r} is {record.get('status')!r}, not pending.")

    record['status'] = 'rejected'
    record['reject_reason'] = reason
    record['rejected_at'] = _now()
    queue['as_of'] = time.strftime('%Y-%m-%d', time.gmtime())
    _atomic_write(queue, path)
    return record
