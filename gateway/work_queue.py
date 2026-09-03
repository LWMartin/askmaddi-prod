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
#
# A THIRD off-ramp (2026-07-07, corpus-floor doctrine):
#   corpus_thin — build_card's corpus floor found too little source material to
#              be worth enriching or gating (EXIT_CORPUS_THIN). Neither a crash
#              (the pipeline ran clean) nor a human ruling (no card ever
#              existed): the factory ABSTAINED. A SOURCING signal: this SKU
#              needs seed-urls and/or the YT transcript leg before retrying.
#              Terminal without retry — the same corpus re-floors identically,
#              so retries would burn build budget deterministically for
#              nothing. Recovery is requeue() after sourcing improves.
STATES = ('resolved', 'building', 'review_ready', 'promoted', 'failed',
          'rejected', 'corpus_thin', 'needs_category')

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

# --- Transient-failure classification (turtle doctrine, 2026-07-15) ---------
# Weather vs defect: a socket timeout or connection reset says nothing about the
# SKU or the pipeline — it says the network had a moment. Burning the 3-attempt
# defect budget on weather is how six launch-week records ended up permanently
# parked in `failed`. A transient failure therefore does NOT increment
# build_attempts; it sets a cooldown and returns the record to `resolved`, where
# claim_next skips it until the cooldown expires ("a timeout can try another
# day"). A separate, generous transient budget (≈ a week of tries at drip pace)
# guards against a genuinely-dead endpoint looping forever — exhausting THAT
# parks the record `failed` with a distinct reason, still visible in /admin.
# Deterministic failures keep the original 3-strike semantics: they are the
# pipeline-defect signal and should stay loud.
TRANSIENT_ERROR_PATTERNS = (
    'socket.timeout', 'timed out', 'timeouterror', 'read timeout',
    'connect timeout', 'connectionerror', 'connection refused',
    'connection reset', 'connection aborted', 'temporary failure in name',
    'http 429', '429 too many', 'http 500', 'http 502', 'http 503',
    'http 504', 'bad gateway', 'service unavailable', 'gateway timeout',
)
DEFAULT_TRANSIENT_COOLDOWN_S = 6 * 3600      # 6h between retries of one SKU
DEFAULT_MAX_TRANSIENT_RETRIES = 28           # ≈ a week of chances at 4/day


def _is_transient(error_detail):
    """True if the error text reads as network weather, not a pipeline defect."""
    text = str(error_detail).lower()
    return any(p in text for p in TRANSIENT_ERROR_PATTERNS)


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
    """Atomic whole-file write (temp in same dir + os.replace), GROUP-RW.

    Build state is a mutable map (status transitions rewrite a record), so this
    uses temp+replace like skus_registry / review_queue — a reader never observes
    a half-written queue.

    CROSS-USER STORE (found live, 2026-07-02 factory startup): this store is
    written by BOTH sides of the pipeline-group seam — the factory (phantomops:
    claim/mark) and the gateway (askmaddi: publish/reject). mkstemp creates
    temps 0600, and os.replace carries that mode onto the store, so every
    rewrite by one user would strip the OTHER user's read and blind either the
    drip or /admin on the next tick. fchmod 0o664 before replace keeps the
    store group-readable/writable across every rewrite (the data/ dir's setgid
    bit keeps it in the pipeline group). skus_registry/review_queue don't need
    this — their stores are written by askmaddi alone.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.wq-', suffix='.tmp')
    try:
        os.fchmod(fd, 0o664)
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
           demand=0, max_attempts=DEFAULT_MAX_ATTEMPTS, path=WORK_QUEUE_PATH):
    """Enroll a freshly-resolved SKU into the build queue at state `resolved`.

    Called by the factory resolve-pass exactly when resolve_proposal() returns
    outcome=='resolved' (a confident pick already written to the spine). The
    record carries precisely what build_card.py needs as input: slug -> --sku-id,
    label -> --sku-label, plus the optional category / seed_urls / aliases / mount.

    `demand` is the proposal's mention/fork weight (resolve_pass forwards fork_n).
    It is stored on the record so claim_next() can build highest-demand-first when
    a resolved BACKLOG exists — the front-of-line lever. Default 0 (a requeue or a
    demand-less enroll sorts after any mention-backed peer, ahead of nothing).

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
        'demand': float(demand or 0),
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


def set_aliases(slug, aliases, *, label=None, path=WORK_QUEUE_PATH):
    """Attach (or replace) fetch aliases on a record STILL AT `resolved`.

    Sibling to set_seed_urls — the second surgical writer on the sourcing
    seam. Aliases flow enroll -> factory argv (--alias, repeated) -> fetch,
    where they widen corpus capture; the optional `label` replace sharpens
    the YT query ("<label> review"). Identity precision is NOT this seam's
    job — that's spine_identity() at assemble (images-on-spine step 4);
    aliases exist to get enough of the right corpus IN for the floor, and
    the human gate holds the line on what a card claims.

    First caller: manfrotto-befree (2026-07-15) — enrolled label-only as
    'Manfrotto Befree', a line name spanning a dozen variants; floored
    corpus_thin on 2026-07-09 with zero aliases to hold variant-relevant
    sources.

    RESOLVED-ONLY, same reasoning as set_seed_urls: a building record's
    inputs are in flight; terminal records re-open via requeue() FIRST.
    Refuses an empty alias list (a clear must be deliberate, not a typo'd
    positional). Fail-closed with a status, never a partial write.

    Returns: 'set' | 'missing-slug' | 'not-resolved' | 'empty-aliases'.
    """
    aliases = [str(a).strip() for a in (aliases or []) if str(a).strip()]
    if not aliases:
        return 'empty-aliases'
    queue = load_queue(path)
    record = queue.get('queue', {}).get(slug)
    if record is None:
        return 'missing-slug'
    if record.get('state') != 'resolved':
        return 'not-resolved'
    record['aliases'] = aliases
    if label is not None and str(label).strip():
        record['label'] = str(label).strip()
    record['aliases_set_at'] = _now()
    queue['as_of'] = _today()
    _atomic_write(queue, path)
    return 'set'


def claim_next(path=WORK_QUEUE_PATH):
    """Atomically claim the highest-demand `resolved` record and mark it `building`.

    The rotation cursor for the drip loop: each tick claims one SKU to build.
    DEMAND-FIRST, then FIFO: records sort by descending `demand` (the mention/fork
    weight enroll stored), tie-broken by enrolled_at so equal-demand peers still
    drain oldest-first. This is the front-of-line lever — a mention-hot SKU that
    resolves into a standing backlog builds (and gets crawlable) before colder
    peers, instead of waiting its turn in pure arrival order. With no backlog
    (the steady state — resolve drips ~1 and the drip drains it) demand and FIFO
    agree, so this is a no-op there and only bites when a batch piles up (a requeue
    sweep, a burst of enrollments). The claim is a single atomic write so two
    overlapping ticks can't grab the same record (last-write-wins at worst
    re-builds one card, never double-promotes — the human gate is unaffected).

    Returns the claimed record (now state=building), or None if nothing is
    resolved-and-waiting.
    """
    queue = load_queue(path)
    q = queue.get('queue', {})

    now = _now()
    resolved = [
        r for r in q.values()
        if r.get('state') == 'resolved'
        # Transient cooldown: ISO-Z strings compare lexicographically, so a
        # plain string comparison is a correct time comparison here.
        and not (r.get('cooldown_until') and r['cooldown_until'] > now)
    ]
    if not resolved:
        return None
    # Demand-first, enrolled_at as the FIFO tie-break. Negate demand for an
    # ascending sort that still puts the largest demand first.
    resolved.sort(key=lambda r: (-float(r.get('demand', 0) or 0),
                                 r.get('enrolled_at', '')))
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
    record.pop('resume_stage', None)
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

    record['last_error'] = str(error_detail)[:500]
    record['last_failed_at'] = _now()

    if _is_transient(error_detail):
        # Weather, not defect: no build_attempts burn. Cool down and let the
        # drip re-claim it later. Only the (large) transient budget can park it.
        record['transient_retries'] = record.get('transient_retries', 0) + 1
        if record['transient_retries'] >= record.get(
                'max_transient_retries', DEFAULT_MAX_TRANSIENT_RETRIES):
            record['state'] = 'failed'
            record['last_error'] = (
                'transient budget exhausted: ' + record['last_error'])[:500]
            record['cooldown_until'] = None
            terminal = True
        else:
            record['state'] = 'resolved'
            record['cooldown_until'] = time.strftime(
                '%Y-%m-%dT%H:%M:%SZ',
                time.gmtime(time.time() + record.get(
                    'transient_cooldown_s', DEFAULT_TRANSIENT_COOLDOWN_S)))
            terminal = False
    else:
        record['build_attempts'] = record.get('build_attempts', 0) + 1
        record['cooldown_until'] = None   # deterministic path never cools down
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


def mark_corpus_thin(slug, detail, *, path=WORK_QUEUE_PATH):
    """Park a building record as `corpus_thin` (build_card's floor abstained).

    NO retry path, deliberately: mark_failed_or_retry retries because
    transient hiccups self-heal across ticks, but a floor verdict is a pure
    function of the corpus — the same sourcing re-floors identically, so
    retries would spend the attempt budget on a deterministic outcome. The
    record parks immediately; requeue() re-opens it once sourcing improves
    (seed-urls added, YT leg enabled). Does NOT increment built_today — an
    abstention is not a build.

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
            f"only a building record can park corpus_thin."
        )

    record['state'] = 'corpus_thin'
    record['last_error'] = str(detail)[:500]
    record['corpus_thin_at'] = _now()
    queue['as_of'] = _today()
    _atomic_write(queue, path)
    return record


def mark_needs_category(slug, detail, *, path=WORK_QUEUE_PATH):
    """Park a building record as `needs_category` (build_card refused to guess).

    The SKU has no authored `facet` in the spine, so no dictionary category
    could be resolved and the build stopped before spending fetch effort.

    NO retry path, on the same reasoning as mark_corpus_thin: the verdict is
    a pure function of the spine, so the next tick re-resolves it
    identically and retries would burn the attempt budget on a settled
    outcome. Unlike corpus_thin, though, what re-opens it is not better
    sourcing but a HUMAN DECISION — someone has to say what this product is.
    That is why this parks visibly instead of failing: a card built as the
    wrong product is worse than a card not built, and the fix is an
    authoring act, not an engineering one. requeue() re-opens the record
    once the facet exists.

    Does NOT increment built_today — a refusal is not a build.

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
            f"only a building record can park needs_category."
        )

    record['state'] = 'needs_category'
    record['last_error'] = str(detail)[:500]
    record['needs_category_at'] = _now()
    queue['as_of'] = _today()
    _atomic_write(queue, path)
    return record


def mark_enrich_partial(slug, detail, *, path=WORK_QUEUE_PATH):
    """Record a budgeted-enrich checkpoint tick: state back to 'resolved'
    (re-claimable next tick), NO attempt burned, resume_stage='enrich' so
    the runner resumes at the checkpoint instead of rebuilding the corpus.

    The factory's fifth verb (2026-07-17): built / retry-or-fail /
    corpus_thin / cooldown said nothing for "the work is big and mid-
    flight" — sony-a7-v burned two full 3-strike budgets for having too
    many reviews. last_error carries the progress line as information,
    not indictment."""
    queue = load_queue(path)
    record = queue['queue'].get(slug)
    if record is None:
        raise KeyError(f"unknown slug: {slug}")
    record['state'] = 'resolved'
    record['resume_stage'] = 'enrich'
    record['last_error'] = str(detail)[:500]
    record['enrich_partial_at'] = _now()
    record['enrich_partial_ticks'] = record.get('enrich_partial_ticks', 0) + 1
    _atomic_write(queue, path)
    return record


_REQUEUEABLE = ('failed', 'rejected', 'corpus_thin', 'needs_category')


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
    record.pop('resume_stage', None)
    record['transient_retries'] = 0
    record['cooldown_until'] = None
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


# Resume points a rebuild can meaningfully start from. 'fetch' means a full
# rebuild and is stored as NO resume_stage (the factory's default plan);
# 'extract' re-runs the gate stack against the cached triples in the spool
# (zero fetch/proxy spend); 'enrich' exists for completeness but is the
# ENRICH_PARTIAL semantic and rarely what a rebuild wants — a changed gate
# lives in extract, so resuming past it would rebuild nothing.
_REBUILD_RESUME_STAGES = ('fetch', 'extract', 'enrich')


def requeue_promoted(slug, *, resume_stage='extract', path=WORK_QUEUE_PATH):
    """Deliberately re-open a `promoted` (live, published) record for rebuild.

    The operator action for "a pipeline gate changed underneath a live card" —
    e.g. the 2026-07-18 relevance-gate enforcement, which left published cards
    carrying corpora admitted under the pre-enforcement gate. requeue()
    deliberately cannot touch promoted records (fat-finger protection for live
    cards); this is the separate, named, deliberate form, and it stamps
    requeued_from='promoted' so the rebuild is auditable on the record.

    Safety properties:
      * THE LIVE CARD IS UNTOUCHED. data/cards/ + browser/ keep serving the
        existing card; only the queue record moves. The rebuilt card replaces
        it only after the human gate approves at /admin/publish (render-then-
        mark, same integrity gate as any publish). A rebuild that exhausts its
        attempt budget parks `failed` with the live card still up — loud in
        /admin, dark nowhere.
      * resume_stage='extract' (the default) rebuilds from the CACHED triples
        in the spool: zero fetch spend, and the decontamination is diffable —
        same evidence pool, current gates. The operator verifies
        <out-root>/<slug>/triples/ exists FIRST; absent triples fail loudly at
        extract's _require and burn an attempt. Pass resume_stage='fetch' (or
        None) for a full re-fetch when triples are gone or a corpus refresh is
        actually wanted.
      * FIFO note: claim_next orders by enrolled_at, so a requeued veteran is
        claimed AHEAD of newer mints. Deliberate — a known-contaminated live
        card outranks a card nobody has seen yet.

    Returns the record. Raises KeyError if absent, ValueError if the record is
    not `promoted` or resume_stage is unknown.
    """
    if resume_stage is not None and resume_stage not in _REBUILD_RESUME_STAGES:
        raise ValueError(
            f"resume_stage {resume_stage!r} not in {_REBUILD_RESUME_STAGES} "
            f"(or None for a full rebuild)."
        )
    queue = load_queue(path)
    q = queue.get('queue', {})
    record = q.get(slug)
    if record is None:
        raise KeyError(f"no work-queue record {slug!r}")
    if record.get('state') != 'promoted':
        raise ValueError(
            f"record {slug!r} is {record.get('state')!r}; requeue_promoted "
            f"only re-opens a live (promoted) record — use requeue() for "
            f"terminal records."
        )

    record['state'] = 'resolved'
    record['build_attempts'] = 0
    record['transient_retries'] = 0
    record['cooldown_until'] = None
    if resume_stage in (None, 'fetch'):
        record.pop('resume_stage', None)
    else:
        record['resume_stage'] = resume_stage
    record['requeued_at'] = _now()
    record['requeued_from'] = 'promoted'
    queue['as_of'] = _today()
    _atomic_write(queue, path)
    return record


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
