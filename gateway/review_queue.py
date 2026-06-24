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
            contamination_key=None, path=REVIEW_QUEUE_PATH):
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
        category=record['category'],
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
