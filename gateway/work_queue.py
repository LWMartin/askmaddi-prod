"""
work_queue.py — the card-factory build-lifecycle state machine.
================================================================================
Demand-factory Piece 2 (card-factory-design-scope, 2026-06-29). The missing layer
between resolve_sku.py (which turns a comparator proposal slug into a buildable
identity in the spine) and build_card.py (which builds one card). This store
tracks the BUILD lifecycle of a resolved-but-not-yet-built SKU:

    resolved -> building -> review_ready -> promoted
                   |
                   +-> failed   (build_card.py exhausted its retries)

WHY THIS IS A NEW STORE, NOT A GRAFT ONTO review_queue (grounded, 2026-06-29):
  review_queue.py tracks ADJUDICATION state (pending -> promoted|rejected): "has a
  human said yes/no to this card." demand_log.py tracks WANT (append-only unmet
  taps). Neither tracks BUILD state. The factory's lifecycle is a categorically
  different axis — it answers "has this resolved SKU been built into a card yet,"
  which is upstream of and orthogonal to "has a human approved it." So this is a
  sibling store, deliberately scoped, mirroring review_queue's atomic-write idiom.

THE ONE-HUMAN-GATE INVARIANT IS PRESERVED:
  This module NEVER promotes anything into the spine. The `promoted` state here is
  OBSERVED, not authored: a built card lands in review_queue as pending, the human
  promotes it through review_queue.promote() (the single sanctioned spine door),
  and observe_promotions() reads that outcome to advance the matching work_queue
  record review_ready -> promoted. There is no second gate; this store watches the
  existing one. A SKU reaching `promoted` here is a REFLECTION of a human decision
  made elsewhere, never a decision made here.

WHERE THE FACTORY TOUCHES THIS STORE (card_factory.py, Piece 3):
  enroll()            on resolve_proposal() outcome=='resolved'  -> state: resolved
  claim_next()        each tick, oldest resolved -> building (the rotation cursor)
  mark_review_ready() build_card.py exit 0                       -> review_ready
  mark_failed_or_retry() build_card.py non-zero                  -> resolved (retry)
                                                                     or failed (exhausted)
  observe_promotions()reads review_queue, review_ready           -> promoted

Records are status-tracked and never deleted (the queue doubles as the build log:
what built, what failed and why, how many attempts). Build state is a mutable map
(a record's status transitions rewrite it), so this uses the same temp-file +
os.replace atomic-write dance as skus_registry / review_queue — a reader never
observes a half-written queue — NOT demand_log's append-only stream.
"""
import json
import os
import tempfile
import time
from pathlib import Path

WORK_QUEUE_PATH = Path(__file__).parent.parent / 'data' / 'work_queue.json'
SCHEMA_VERSION = '0.1.0'

# The build lifecycle. A record advances strictly forward through the happy path
# (resolved -> building -> review_ready -> promoted). There are TWO terminal
# off-ramps, deliberately distinct so the cockpit can tell a mechanical breakdown
# from a human judgment:
#   failed   — build_card.py exhausted its retries (the FACTORY broke). A pipeline
#              health signal: something in fetch/extract/enrich/assemble is wrong.
#   rejected — a CLEAN build the human declined to publish at /admin (the CARD was
#              judged not good enough). A content-quality signal, NOT a crash.
# Conflating these would poison the cockpit's analytics: "the factory is breaking"
# and "the cards aren't good enough" are different problems with different fixes.
STATES = ('resolved', 'building', 'review_ready', 'promoted', 'failed', 'rejected')

# Structured reject reasons for a human-declined clean card — same discipline as
# review_queue.REJECT_REASONS: a reject is a controlled-vocab BUG REPORT against
# the generation pipeline (which stage produced something publish-unworthy), not
# freetext and not "this card needed editing." The review surface is a judgment
# surface, never an editing surface.
CARD_REJECT_REASONS = (
    'thin_sources',     # too few / low-quality sources → sparse card
    'bad_synthesis',    # synthesis garbled, off-tone, or incoherent
    'wrong_identity',   # built the wrong product despite a confident resolve
    'stale_pricing',    # price block implausible / missing / stale-wrong
    'duplicate',        # already covered by an existing live card
    'other',            # escape hatch — prefer a specific code
)

# Default retry budget: how many times the factory re-attempts a build that exits
# non-zero before parking the record in `failed` for human inspection in /admin.
# A transient fetch/enrich hiccup self-heals across ticks; a persistently-failing
# SKU stops consuming cap and surfaces as a pipeline-bug signal, not an infinite
# retry loop. Tunable per-enroll via max_attempts.
DEFAULT_MAX_ATTEMPTS = 3


def _empty_queue():
    return {
        '_description': (
            'Card-factory build-lifecycle state machine. resolved -> building -> '
            'review_ready -> promoted (+failed). Sibling to review_queue (which '
            'tracks human adjudication) and demand_log (which tracks want). This '
            'store NEVER promotes into the spine; `promoted` is observed from '
            'review_queue, never authored here. Status-tracked, never deleted.'
        ),
        'version': SCHEMA_VERSION,
        'as_of': time.strftime('%Y-%m-%d', time.gmtime()),
        # Daily-cap bookkeeping lives IN this store (single source, no separate
        # counter file to desync). built_today auto-resets when cap_date rolls.
        'cap_date': time.strftime('%Y-%m-%d', time.gmtime()),
        'built_today': 0,
        'queue': {},
    }


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _today():
    return time.strftime('%Y-%m-%d', time.gmtime())


def load_queue(path=WORK_QUEUE_PATH):
    """Return the queue dict, or a fresh empty one if absent.

    Tolerant of a missing file (first run), not of a corrupt one — a malformed
    work_queue.json is a real error the caller should see, not silently
    overwrite. Same discipline as review_queue.load_queue / load_registry.
    """
    path = Path(path)
    if not path.exists():
        return _empty_queue()
    return json.loads(path.read_text(encoding='utf-8'))


def _atomic_write(queue, path=WORK_QUEUE_PATH):
    """Atomic whole-file write (temp in same dir + os.replace).

    Build state is a mutable map (status transitions rewrite a record), so this
    uses temp+replace like skus_registry / review_queue — a reader never observes
    a half-written queue.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.wq-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(queue, fh, indent=2, ensure_ascii=False)
            fh.write('\n')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _roll_cap_if_new_day(queue):
    """Reset built_today when the date has rolled. Mutates queue in place.

    The daily cap is a per-UTC-day budget; crossing midnight zeroes the counter so
    a fresh day's drip starts clean. Called on every cap-touching operation so the
    rollover is lazy (no cron needed to reset it).
    """
    today = _today()
    if queue.get('cap_date') != today:
        queue['cap_date'] = today
        queue['built_today'] = 0


def enroll(slug, label, category, *, seed_urls=None, aliases=None, mount=None,
           max_attempts=DEFAULT_MAX_ATTEMPTS, path=WORK_QUEUE_PATH):
    """Enroll a freshly-resolved SKU into the build queue at state `resolved`.

    Called by the factory resolve-pass exactly when resolve_proposal() returns
    outcome=='resolved' (a confident pick already written to the spine). The
    record carries precisely what build_card.py needs as input: slug -> --sku-id,
    label -> --sku-label, plus the optional category / seed_urls / aliases / mount.

    IDEMPOTENT and FORWARD-ONLY: a slug already in the queue is returned untouched,
    regardless of its state. We never re-enroll a SKU that's building, review_ready,
    promoted, or failed — re-resolving an already-known product must not reset its
    build progress or silently re-queue a card a human already adjudicated. (A
    genuinely stuck `failed` record is re-opened deliberately via requeue_failed(),
    not by a blind re-enroll.)

    Returns the record dict.
    """
    queue = load_queue(path)
    q = queue.setdefault('queue', {})

    existing = q.get(slug)
    if existing is not None:
        # Already known at some lifecycle stage — idempotent, forward-only.
        return existing

    record = {
        'slug': slug,
        'label': label,
        'category': category,
        'seed_urls': seed_urls,
        'aliases': list(aliases) if aliases else [],
        'mount': mount,
        'state': 'resolved',
        'build_attempts': 0,
        'max_attempts': int(max_attempts),
        'enrolled_at': _now(),
        'last_error': None,
    }
    q[slug] = record
    queue['as_of'] = _today()
    _atomic_write(queue, path)
    return record


def set_seed_urls(slug, seed_urls_path, *, path=WORK_QUEUE_PATH):
    """Attach (or replace) a seed-urls file on a record STILL AT `resolved`.

    The seam between sourcing and the drip. enroll() is deliberately
    idempotent/forward-only, so the 9 mint-discovered records enrolled
    seed-less (2026-06-30 sourcing gap) cannot receive seeds by re-enrolling.
    This is the surgical writer for that: hand-curated seed files today, the
    seed producer's write point tomorrow — one seam, two callers, same
    contract (build_card consumes `--seed-urls <path>` either way).

    RESOLVED-ONLY: refuses any record past `resolved`. A `building` record's
    inputs are already in flight; retargeting review_ready/promoted would
    silently desync the built card from its recorded inputs; a failed record
    is re-opened deliberately via requeue_failed() FIRST, then re-seeded.
    Fail-closed with a status, never a partial write.

    Returns: 'set' | 'missing-slug' | 'not-resolved'.
    """
    queue = load_queue(path)
    record = queue.get('queue', {}).get(slug)
    if record is None:
        return 'missing-slug'
    if record.get('state') != 'resolved':
        return 'not-resolved'
    record['seed_urls'] = str(seed_urls_path)
    record['seeds_set_at'] = _now()
    queue['as_of'] = _today()
    _atomic_write(queue, path)
    return 'set'


def claim_next(path=WORK_QUEUE_PATH):
    """Atomically claim the oldest `resolved` record and mark it `building`.

    The rotation cursor for the drip loop: each tick claims one SKU to build. FIFO
    by enrolled_at so the queue drains in order (oldest demand first), and the
    claim is a single atomic write so two overlapping ticks can't grab the same
    record (last-write-wins at worst re-builds one card, never double-promotes —
    the human gate downstream is unaffected).

    Returns the claimed record (now state=building), or None if nothing is
    resolved-and-waiting.
    """
    queue = load_queue(path)
    q = queue.get('queue', {})

    resolved = [r for r in q.values() if r.get('state') == 'resolved']
    if not resolved:
        return None
    resolved.sort(key=lambda r: r.get('enrolled_at', ''))
    record = resolved[0]

    record['state'] = 'building'
    record['build_started_at'] = _now()
    queue['as_of'] = _today()
    _atomic_write(queue, path)
    return record


def mark_review_ready(slug, *, path=WORK_QUEUE_PATH):
    """Advance a `building` record to `review_ready` after a clean build_card exit.

    Also increments built_today (rolling the cap if the day changed) — a clean
    build is exactly the event the daily cap counts. The card itself has, by this
    point, landed in review_queue as pending (build_card's render stage); this
    state transition records that the factory's job for this SKU is done and the
    human gate now owns it.

    Returns the record. Raises KeyError if absent, ValueError if not `building`.
    """
    queue = load_queue(path)
    q = queue.get('queue', {})
    record = q.get(slug)
    if record is None:
        raise KeyError(f"no work-queue record {slug!r}")
    if record.get('state') != 'building':
        raise ValueError(
            f"record {slug!r} is {record.get('state')!r}, not building — "
            f"only a building record can become review_ready."
        )

    record['state'] = 'review_ready'
    record['built_at'] = _now()
    _roll_cap_if_new_day(queue)
    queue['built_today'] = queue.get('built_today', 0) + 1
    queue['as_of'] = _today()
    _atomic_write(queue, path)
    return record


def mark_failed_or_retry(slug, error_detail, *, path=WORK_QUEUE_PATH):
    """Handle a non-zero build_card exit: retry until the budget is exhausted.

    Increments build_attempts. If attempts remain (< max_attempts) the record
    drops back to `resolved` so the next tick re-claims and re-attempts it — a
    transient fetch/enrich hiccup self-heals across ticks. When the budget is
    exhausted the record parks in `failed`, stops consuming cap, and surfaces in
    /admin as a pipeline-bug signal (a SKU that persistently won't build is a
    defect to inspect, not an infinite loop). A failed build does NOT increment
    built_today — only a clean build counts against the daily cap.

    Returns (record, terminal) where terminal is True iff the record is now
    `failed`. Raises KeyError if absent, ValueError if not `building`.
    """
    queue = load_queue(path)
    q = queue.get('queue', {})
    record = q.get(slug)
    if record is None:
        raise KeyError(f"no work-queue record {slug!r}")
    if record.get('state') != 'building':
        raise ValueError(
            f"record {slug!r} is {record.get('state')!r}, not building — "
            f"only a building record can fail or retry."
        )

    record['build_attempts'] = record.get('build_attempts', 0) + 1
    record['last_error'] = str(error_detail)[:500]
    record['last_failed_at'] = _now()

    if record['build_attempts'] >= record.get('max_attempts', DEFAULT_MAX_ATTEMPTS):
        record['state'] = 'failed'
        terminal = True
    else:
        # Back to the resolved pool for the next tick to re-claim.
        record['state'] = 'resolved'
        terminal = False

    queue['as_of'] = _today()
    _atomic_write(queue, path)
    return record, terminal


def mark_published(slug, *, path=WORK_QUEUE_PATH):
    """Advance a `review_ready` record to `promoted` — the human published it.

    This is the happy-path terminal transition, driven by the /admin PUBLISH
    action: the human looked at the built card preview, approved it, and the
    surface ran build_site.py to render it into the live browser/ surface. By the
    time this is called the card IS live; this records that the human's one
    in-the-loop decision (the render approval) has been made.

    The publish IS the human gate. Unlike the slug-straggler path (where
    review_queue.promote writes a slug into the spine), a factory card's identity
    was already settled at confident resolve; the only human judgment here is
    "publish this card, yes/no." So this is a direct work_queue transition, not an
    observation of review_queue — the two adjudications are different in kind.

    A promoted record stays in the queue as the build log (it built, it shipped).

    Returns the record. Raises KeyError if absent, ValueError if not review_ready.
    """
    queue = load_queue(path)
    q = queue.get('queue', {})
    record = q.get(slug)
    if record is None:
        raise KeyError(f"no work-queue record {slug!r}")
    if record.get('state') != 'review_ready':
        raise ValueError(
            f"record {slug!r} is {record.get('state')!r}, not review_ready — "
            f"only a review_ready card can be published."
        )

    record['state'] = 'promoted'
    record['published_at'] = _now()
    queue['as_of'] = _today()
    _atomic_write(queue, path)
    return record


def reject_card(slug, reason, *, path=WORK_QUEUE_PATH):
    """Reject a CLEAN built card the human declined to publish at /admin.

    Distinct from mark_failed_or_retry (which handles a build_card CRASH). This is
    the human judging a successfully-built card not good enough to go live — a
    content-quality decision, not a mechanical failure. `reason` must be in
    CARD_REJECT_REASONS: a reject is a structured pipeline-bug signal routed
    upstream to the generation code, never a card-level edit (the review surface
    is a judgment surface). Nothing is published to browser/.

    The record parks in `rejected` (terminal, retained as the adjudication log).
    A rejected card stops consuming attention; the defect it signals is fixed in
    the pipeline, then the SKU can be re-opened with requeue_rejected().

    Returns the record. Raises KeyError if absent, ValueError if not review_ready
    or if reason is not a recognized code.
    """
    if reason not in CARD_REJECT_REASONS:
        raise ValueError(
            f"card reject reason {reason!r} not in {CARD_REJECT_REASONS} — a "
            f"reject is a structured pipeline-bug signal, not freetext."
        )
    queue = load_queue(path)
    q = queue.get('queue', {})
    record = q.get(slug)
    if record is None:
        raise KeyError(f"no work-queue record {slug!r}")
    if record.get('state') != 'review_ready':
        raise ValueError(
            f"record {slug!r} is {record.get('state')!r}, not review_ready — "
            f"only a review_ready card can be rejected at the publish gate."
        )

    record['state'] = 'rejected'
    record['reject_reason'] = reason
    record['rejected_at'] = _now()
    queue['as_of'] = _today()
    _atomic_write(queue, path)
    return record


_REQUEUEABLE = ('failed', 'rejected')


def requeue(slug, *, path=WORK_QUEUE_PATH):
    """Deliberately re-open a terminal (`failed` or `rejected`) record to `resolved`.

    The escape hatch for "the upstream bug is fixed, try this SKU again" — whether
    the SKU previously crashed the build (`failed`) or built a card the human
    declined (`rejected`). Distinct from enroll's idempotent no-op (which refuses
    to touch a terminal record), this is an explicit operator action that resets
    attempts and re-queues for a fresh build. Only a terminal record can be
    requeued — re-opening an in-flight record is meaningless.

    Returns the record. Raises KeyError if absent, ValueError if not terminal.
    """
    queue = load_queue(path)
    q = queue.get('queue', {})
    record = q.get(slug)
    if record is None:
        raise KeyError(f"no work-queue record {slug!r}")
    if record.get('state') not in _REQUEUEABLE:
        raise ValueError(
            f"record {slug!r} is {record.get('state')!r}; requeue only re-opens "
            f"a terminal record {_REQUEUEABLE}."
        )

    prior = record['state']
    record['state'] = 'resolved'
    record['build_attempts'] = 0
    record['reject_reason'] = None
    record['requeued_at'] = _now()
    record['requeued_from'] = prior
    queue['as_of'] = _today()
    _atomic_write(queue, path)
    return record


# Back-compat alias: earlier code/tests referenced requeue_failed before `rejected`
# existed. requeue() is the general form; this name is retained as a thin wrapper.
def requeue_failed(slug, *, path=WORK_QUEUE_PATH):
    """Deprecated alias for requeue(). Re-opens a failed (or rejected) record."""
    return requeue(slug, path=path)


def cap_remaining(cap, *, path=WORK_QUEUE_PATH):
    """How many more cards may build today under `cap` (rolls the day lazily).

    Returns max(0, cap - built_today) after a date-rollover check. The factory
    calls this each tick to decide whether to claim+build or sleep. Writes back
    only if the rollover actually reset the counter (avoids a needless write on
    the common same-day path).
    """
    queue = load_queue(path)
    before = (queue.get('cap_date'), queue.get('built_today'))
    _roll_cap_if_new_day(queue)
    if (queue.get('cap_date'), queue.get('built_today')) != before:
        _atomic_write(queue, path)
    return max(0, int(cap) - queue.get('built_today', 0))


def counts(path=WORK_QUEUE_PATH):
    """State histogram + cap status for the /admin cockpit informatics.

    Returns {'resolved': n, 'building': n, 'review_ready': n, 'promoted': n,
    'failed': n, 'built_today': n, 'cap_date': 'YYYY-MM-DD', 'total': n}. The
    factory-health view the design note earmarked for /admin (daily build counts,
    queue depths) reads straight off this.
    """
    queue = load_queue(path)
    q = queue.get('queue', {})
    hist = {s: 0 for s in STATES}
    for record in q.values():
        st = record.get('state')
        if st in hist:
            hist[st] += 1
    hist['built_today'] = queue.get('built_today', 0)
    hist['cap_date'] = queue.get('cap_date', _today())
    hist['total'] = len(q)
    return hist


def get(slug, path=WORK_QUEUE_PATH):
    """Fetch one record by slug (None if absent)."""
    return load_queue(path).get('queue', {}).get(slug)


def load_by_state(state, path=WORK_QUEUE_PATH):
    """Return all records in a given state (e.g. 'failed' for the /admin panel)."""
    if state not in STATES:
        raise ValueError(f"state {state!r} not in {STATES}")
    q = load_queue(path).get('queue', {})
    return [r for r in q.values() if r.get('state') == state]
