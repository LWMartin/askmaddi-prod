"""
resolve_pass.py — the demand-factory resolve pass (proposals -> work_queue).
================================================================================
The bridge between the comparator demand miner (phantom-ops, Gemma, box) and the
card factory's build queue (askmaddi-prod). A discrete pass that takes a batch of
comparator proposals, runs each through resolve_sku.resolve_proposal() (Piece 1),
and ENROLLS the confidently-resolved ones into the work_queue (Piece 2) so the
card_factory drip (Piece 3) can build them.

WHERE THIS SITS IN THE FACTORY (card-factory-design-scope, 2026-06-29):

    comparator_mine_typed.proposals()   [phantom-ops, box: Gemma types every clause]
        emits proposals.json: [{"slug": "...", "fork_n": N}, ...]
              │
              ▼
    resolve_pass.run()                   [THIS module, box: eBay + Gemma disambig]
        per slug -> resolve_proposal():
          outcome 'resolved'     -> work_queue.enroll()   [state: resolved]  ← the drip's input
          outcome 'queued'       -> already in review_queue (low-confidence straggler)
          outcome 'no_candidate' -> already in demand_log (unmet want)
              │
              ▼
    card_factory.tick()                  [Piece 3: drains the work_queue]

WHY A DISCRETE PASS, NOT FUSED INTO THE DRIP (decision 4, design note):
  "Resolve-and-cache as a discrete pass; rotational 24/7 drip consumes it." Resolve
  is a different cadence than build: resolution is a cheap eBay+Gemma round-trip
  done in a batch when fresh proposals arrive; building is the slow 24/7 trickle.
  Decoupling them means the drip always has a ready queue to pull from and a resolve
  failure never stalls the build loop (and vice versa).

WHY enroll ONLY THE 'resolved' OUTCOME:
  The other two outcomes are already correctly homed by resolve_proposal itself —
  'queued' landed in review_queue (a human will pick the listing), 'no_candidate'
  landed in demand_log (unmet want, expansion signal). Enrolling those into the
  build queue would be wrong: there's nothing confidently-resolved to build yet.
  Only a confident resolve is a buildable SKU. The pass is a router, not a second
  resolver — it trusts resolve_proposal's routing and acts on exactly one outcome.

INJECTED COLLABORATORS (offline-testable, live on the box):
  resolve_proposal needs ebay + gemma (box-only). run() takes them injected, plus
  an injected `resolve_fn` (defaults to resolve_sku.resolve_proposal) so the pass's
  batch/routing/enroll logic is unit-tested offline with a fake resolver, and the
  real eBay+Gemma resolution is proven on the VPS. Same discipline throughout.

This module writes ONLY through work_queue.enroll (and indirectly through
resolve_proposal's existing primitives). It never touches the spine directly and
never publishes. Idempotent: enroll de-dups, so re-running the pass over the same
proposals does not double-queue.
"""
import argparse
import datetime
import json
import os
import sys
from pathlib import Path

# --- sibling-import bootstrap (cron robustness) -------------------------------
# resolve_pass is the cron entry point of the emit->resolve chain, invoked across
# two service users on the spool path. The gateway modules use flat sibling
# imports (import resolve_sku, import work_queue) — the repo-wide convention,
# which Python satisfies via sys.path[0] ONLY when the file is run as a direct
# script (python3 /abs/gateway/resolve_pass.py). It FAILS under `python3 -m
# gateway.resolve_pass` (repo root goes on the path, not gateway/) and when this
# module is imported from elsewhere. Putting this file's own directory on
# sys.path makes the flat imports resolve under every invocation style without
# changing the convention the other modules rely on. Idempotent (guarded), and a
# no-op when already first on the path (the direct-script case).
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import resolve_sku       # noqa: E402  (after the path bootstrap above)
import work_queue        # noqa: E402


# --- attempt ledger (head-of-line-block cure) --------------------------------
# The pass paces NEW resolve attempts with --max, and skips slugs already in the
# work_queue (`already_enrolled`). But the outcomes that DON'T land in the queue —
# no_candidate (-> demand_log), queued (-> review_queue), error — leave no trace
# the next run can skip. So under a small --max the SAME unproductive head slugs
# (a perennial eBay-miss like `ulanzi-f38`, or a proposal whose resolved identity
# is already built) burn the whole budget every run and the 200-long tail NEVER
# gets attempted — the factory starves with a full proposals.json. This ledger
# records each non-productive attempt with a timestamp; a slug attempted within
# RETRY_TTL_DAYS is skipped WITHOUT spending a paced slot, so the budget always
# advances to fresh candidates. A stale entry (past the TTL) lets a no_candidate
# retry later — eBay inventory changes. A `resolved` outcome graduates the slug
# out of the ledger. Opt-in: run() only touches the ledger when a path is given,
# so tests and one-shot callers keep the exact legacy behaviour (like max_new).
ATTEMPTS_LEDGER_PATH = str(Path(__file__).resolve().parent.parent / 'data' / 'resolve-attempts.json')
RETRY_TTL_DAYS = 7

# Decontamination zone. A no_candidate/queued/error is TRANSIENT (eBay inventory
# may appear later), so it cools for RETRY_TTL_DAYS then retries. But a proposal
# that RESOLVES to a canonical already in the queue is a STRUCTURAL dead-end — the
# card exists; a rename/mint made its proposal slug differ from the built slug, so
# it slips the pre-resolve skip and re-blocks the paced head forever. That never
# becomes productive, so it is escorted here PERMANENTLY: the ledger stores this
# sentinel instead of a timestamp and _cooling skips it with no TTL. Visible on
# `cat data/resolve-attempts.json` as `"<slug>": "decontaminated"`.
DECONTAM_MARK = 'decontaminated'


def _load_ledger(path):
    """Read the attempt ledger {slug: last_attempt_iso}. Tolerant: a missing or
    corrupt ledger returns {} — it paces retries, never correctness, so a bad
    file degrades to 'attempt everything' rather than crashing the cron."""
    try:
        with open(path) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_ledger(ledger, path):
    """Atomically persist the ledger (temp + rename in the same dir). Tolerant: a
    write failure is swallowed — losing the ledger costs one round of redundant
    attempts, never a wrong build."""
    try:
        tmp = f"{path}.tmp"
        with open(tmp, 'w') as fh:
            json.dump(ledger, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass


def _cooling(ledger, slug, now, ttl_days):
    """True if `slug` should be skipped: either escorted to the decontamination
    zone (permanent) or attempted within ttl_days of `now` (transient cool)."""
    ts = ledger.get(slug)
    if not ts:
        return False
    if ts == DECONTAM_MARK:
        return True  # structural dead-end (already-built canonical) -> skip forever
    try:
        last = datetime.datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
    except ValueError:
        return False  # unparseable stamp -> treat as stale, allow a retry
    return (now - last) < datetime.timedelta(days=ttl_days)


def load_proposals(path):
    """Read a proposals artifact emitted by the comparator miner.

    Accepts either:
      - a JSON list of objects: [{"slug": "...", "fork_n": N}, ...]
        (optionally with "vendor"/"model" — the minting-wire identity shape)
      - a JSON list of [fork_n, slug, ...] tuples (proposals() native shape)
    Returns a normalized list of {'slug': str, 'fork_n': int, 'vendor': str|None,
    'model': str|None} dicts, sorted by fork_n descending (highest demand first —
    the drip builds the most-wanted products soonest). vendor/model are None on
    the legacy tuple/dict shapes; present only on the identity shape, where they
    let resolve_proposal MINT a slug that isn't yet a registry entry.

    Raises ValueError on a malformed artifact (a corrupt proposals file is a real
    error the operator should see, not a silently-empty pass).
    """
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(raw, list):
        raise ValueError(
            f"proposals artifact must be a JSON list, got {type(raw).__name__}")

    out = []
    for item in raw:
        vendor = model = None
        gtin = mpn = product_url = None
        if isinstance(item, dict):
            slug = item.get('slug')
            fork_n = int(item.get('fork_n', 0))
            # Identity-shape rows (minting wire) carry vendor+model so a slug not
            # yet in the registry can be MINTED at resolve time. Absent on legacy
            # rows -> stays None -> resolve_proposal can enrich but not mint.
            vendor = item.get('vendor') or None
            model = item.get('model') or None
            # GTIN/MPN-first identity (spec step 2): the feed's identifiers ride
            # the proposal so resolve_multisource can do a deterministic id join
            # ahead of the keyword+Gemma pick. Absent on legacy rows -> None ->
            # the ladder escalates past the deterministic rung, unchanged.
            gtin = item.get('gtin') or None
            mpn = item.get('mpn') or None
            product_url = item.get('product_url') or None
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            # proposals() native tuple: (fork_n, comp_id, pos_n, abs_n)
            fork_n = int(item[0])
            slug = item[1]
        else:
            raise ValueError(f"unrecognized proposal entry: {item!r}")
        if not slug:
            raise ValueError(f"proposal entry missing slug: {item!r}")
        out.append({'slug': slug, 'fork_n': fork_n,
                    'vendor': vendor, 'model': model,
                    'gtin': gtin, 'mpn': mpn, 'product_url': product_url})

    out.sort(key=lambda d: d['fork_n'], reverse=True)
    return out


def run(proposals, *, ebay, gemma, demand_log, review_queue,
        resolve_fn=None, skus_path=None, work_queue_path=None,
        floor=None, on_event=None, max_new=None,
        attempts_ledger_path=None, retry_ttl_days=RETRY_TTL_DAYS, now=None):
    """Run the resolve pass over a batch of proposals. Returns a summary dict.

    Parameters
    ----------
    proposals : list[dict]
        Normalized proposals ({'slug', 'fork_n'}), e.g. from load_proposals().
    ebay, gemma, demand_log, review_queue :
        The injected collaborators resolve_proposal needs (production: ebay_api,
        a GemmaDisambiguator, the demand_log + review_queue modules).
    resolve_fn : callable
        The resolver entrypoint, defaulting to resolve_sku.resolve_proposal.
        Injected so the pass is unit-tested with a fake resolver offline.
    skus_path, work_queue_path, floor :
        Optional overrides threaded to resolve_proposal / enroll (tests pass tmp
        paths; production uses the module defaults).
    on_event : callable(dict) | None
        Optional per-proposal callback for progress/logging.

    For each proposal:
      - call resolve_fn(slug, ...) -> outcome dict
      - outcome 'resolved' -> look up the SKU's build identity and enroll() it
      - outcome 'queued' / 'no_candidate' -> count it; already homed by the resolver
      - ResolveError (no registry entry) -> count as 'error', keep going (one bad
        proposal must not abort the batch); eBay API errors propagate (a transient
        network problem is not a per-proposal routing decision).

    attempts_ledger_path, retry_ttl_days, now :
        Head-of-line-block cure. When attempts_ledger_path is set, a slug whose
        last non-productive attempt is younger than retry_ttl_days is skipped
        without spending a paced slot (so --max always advances to fresh
        candidates). `now` is injectable for tests (default: utcnow, tz-aware).
        attempts_ledger_path=None disables the ledger (legacy: retry every slug).

    Returns:
      {'total': n, 'enrolled': n, 'already_queued': n, 'no_candidate': n,
       'errors': n, 'skipped_enrolled': n, 'skipped_cooldown': n,
       'decontaminated': n, 'deferred': n,
       'enrolled_slugs': [...], 'error_slugs': [...]}
    """
    # The full GTIN/MPN-first ladder A->E (spec maddi-multisource-identity-matcher).
    # resolve_multisource WRAPS resolve_proposal (rung A registry + rung B eBay+Gemma,
    # id gate live inside it) and escalates a genuine eBay MISS to the source rungs:
    # C mfr/Shopify surface, D Icecat/Wikidata cross-confirm, E Adorama in-stock
    # catalogue (offline index lookup, carries the buyable CTA). ACTIVATED 2026-09-01
    # after reviewing the id-gate's review-queue volume (19 promoted / 3 rejected /
    # 43 pending — a healthy ~14% reject rate, not a false-positive flood) and adding
    # rung E. The source rungs self-gate on identity presence (C needs a source_url,
    # D a join key, E a gtin/mpn/brand+model), so the cron's support/unknown misses
    # no-op cheaply; only real-product misses fire live fetches. env_bootstrap loads
    # ICECAT_* / EBAY_* in __main__, so C/D/E run live under the plain crontab.
    resolve_fn = resolve_fn or resolve_sku.resolve_multisource
    skus_path = skus_path or resolve_sku.skus_registry.SKUS_PATH

    summary = {
        'total': 0, 'enrolled': 0, 'already_queued': 0,
        'no_candidate': 0, 'errors': 0, 'skipped_enrolled': 0,
        'skipped_cooldown': 0, 'decontaminated': 0, 'deferred': 0,
        'duplicate_identity': 0,
        'enrolled_slugs': [], 'error_slugs': [],
    }

    # Attempt ledger (opt-in): retires recently-attempted, non-productive slugs
    # from the paced budget so the tail resolves instead of the head re-blocking.
    ledger = _load_ledger(attempts_ledger_path) if attempts_ledger_path else {}
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now.strftime('%Y-%m-%dT%H:%M:%SZ')

    # PACING: skip proposals already in the work_queue at ANY lifecycle state,
    # BEFORE the (non-free) eBay + LLM calls inside resolve_proposal. enroll() is
    # already idempotent, but re-resolving the whole backlog every tick is exactly
    # the sustained LLM load that OOM-killed the disambiguator. With max_new set,
    # each invocation resolves at most that many NEW slugs, so a paced 2-hourly
    # cron spreads the load instead of hammering ~200 in one batch. The membership
    # set is read ONCE (not per-proposal). max_new=None preserves legacy behaviour
    # (resolve everything, unbounded) for the one-shot / test callers.
    already_enrolled = set()
    try:
        wq = (work_queue.load_queue(work_queue_path) if work_queue_path
              else work_queue.load_queue())
        already_enrolled = set(wq.get('queue', {}).keys())
    except (OSError, ValueError):
        pass  # no queue yet (first run) -> nothing to skip
    new_resolved = 0

    # resolve_proposal kwargs that are only passed when overridden (tests).
    rp_kwargs = {'ebay': ebay, 'gemma': gemma,
                 'demand_log': demand_log, 'review_queue': review_queue,
                 'skus_path': skus_path}
    if floor is not None:
        rp_kwargs['floor'] = floor

    for prop in proposals:
        slug = prop['slug']

        # Already in the build queue -> skip the expensive resolve (idempotent
        # anyway; this just avoids re-hammering eBay/LLM on the standing backlog).
        if slug in already_enrolled:
            summary['skipped_enrolled'] += 1
            continue
        # Recently attempted but non-productive (no_candidate/queued/error) ->
        # skip WITHOUT spending a paced slot, so the budget advances to fresh
        # candidates. Checked BEFORE the max_new gate so a cooling head slug is
        # neither resolved nor counted as deferred. The TTL lets it retry later.
        if attempts_ledger_path and _cooling(ledger, slug, now, retry_ttl_days):
            summary['skipped_cooldown'] += 1
            continue
        # Per-invocation cap on NEW resolves: once hit, count the rest as deferred
        # (a later tick picks them up) instead of resolving the whole file at once.
        if max_new is not None and new_resolved >= max_new:
            summary['deferred'] += 1
            continue
        new_resolved += 1

        summary['total'] += 1
        event = {'slug': slug, 'fork_n': prop.get('fork_n')}

        # Per-proposal identity (minting wire): carried on the identity shape so a
        # not-yet-registered slug can be minted. None on legacy shapes -> enrich
        # only. Passed per-proposal, so it's added to a copy of the shared kwargs.
        call_kwargs = dict(rp_kwargs)
        if prop.get('vendor'):
            call_kwargs['vendor'] = prop['vendor']
        if prop.get('model'):
            call_kwargs['model'] = prop['model']
        # Forward the feed identity so resolve_multisource can join on it.
        if prop.get('gtin'):
            call_kwargs['gtin'] = prop['gtin']
        if prop.get('mpn'):
            call_kwargs['mpn'] = prop['mpn']
        if prop.get('product_url'):
            call_kwargs['product_url'] = prop['product_url']

        try:
            outcome = resolve_fn(slug, **call_kwargs)
        except resolve_sku.ResolveError as e:
            # No registry entry — an upstream bug (proposed something unregistered).
            # Count and continue; one bad proposal does not abort the batch.
            summary['errors'] += 1
            summary['error_slugs'].append(slug)
            event.update(outcome='error', detail=str(e))
            if attempts_ledger_path:
                ledger[slug] = now_iso  # unregistered -> TTL-skip until fixed
            if on_event:
                on_event(event)
            continue

        kind = outcome.get('outcome')
        event['outcome'] = kind

        if kind == 'resolved':
            # Confident resolve -> a buildable SKU. Enroll it into the work queue.
            # The build identity (label, category, aliases, mount) comes from the
            # registry entry the resolver just enriched OR minted; re-look it up
            # rather than widen resolve_proposal's tested return contract.
            #
            # Use the RESOLVED slug from the outcome, not the proposed loop slug:
            # a mint can freeze the slug to a hand-authored form (resolve_slug),
            # so the registry entry now lives under outcome['slug']. After a mint,
            # upsert() ran, so lookup_proposal succeeds on that slug.
            resolved_slug = outcome.get('slug', slug)
            # Canonical-slug re-block guard. The pre-resolve skip tests the PROPOSAL
            # slug against `already_enrolled` (canonical queue keys), so a proposal
            # whose canonical differs (rename/mint: `sony-a7r-original` -> `sony-a7r`,
            # `sigma-35-f12-dg-dn` -> `sigma-35mm-f1-2-dg-dn-art`) slips past it,
            # re-resolves every tick, and — counted as a fresh `enrolled` AND popped
            # from the ledger below — can NEVER be cooled. Two such zombies at the
            # head ate the whole paced --max budget, leaving `resolved` empty and the
            # factory starved. If the RESOLVED slug is already queued, this resolve
            # built nothing new: treat it like `already_queued` and cool the PROPOSAL
            # slug so the next tick skips it cheaply and the budget reaches the tail.
            if resolved_slug in already_enrolled:
                summary['decontaminated'] += 1
                event['outcome'] = 'decontaminated'
                event['resolved_slug'] = resolved_slug
                if attempts_ledger_path:
                    # Permanent escort (not a TTL cool): the card exists, so this
                    # proposal slug never becomes productive. See DECONTAM_MARK.
                    ledger[slug] = DECONTAM_MARK
            else:
                ident = resolve_sku.lookup_proposal(resolved_slug, skus_path=skus_path)
                enroll_kwargs = {}
                if work_queue_path is not None:
                    enroll_kwargs['path'] = work_queue_path
                work_queue.enroll(
                    resolved_slug, ident['label'], ident['category'],
                    aliases=ident.get('aliases'),
                    demand=prop.get('fork_n', 0),  # front-of-line weight
                    **enroll_kwargs,
                )
                summary['enrolled'] += 1
                summary['enrolled_slugs'].append(resolved_slug)
                # Guard the cap against duplicate/fork proposals of the same product:
                # a later fork_n of a just-enrolled slug now skips instead of spending
                # another slot on a product already queued.
                already_enrolled.add(resolved_slug)
                # Graduated: drop any stale non-productive marks for this identity so
                # it is never spuriously cooled off after a successful resolve.
                if attempts_ledger_path:
                    ledger.pop(slug, None)
                    ledger.pop(resolved_slug, None)
        elif kind == 'queued':
            # Low-confidence -> already in review_queue as a straggler. Not built.
            summary['already_queued'] += 1
            if attempts_ledger_path:
                ledger[slug] = now_iso  # non-productive -> cool off for the TTL
        elif kind == 'no_candidate':
            # Unmet want -> already in demand_log. Not built.
            summary['no_candidate'] += 1
            if attempts_ledger_path:
                ledger[slug] = now_iso  # perennial eBay-miss -> cool off, retry later
        elif kind == 'duplicate_identity':
            # Cross-slug product-identity dup: this proposal's resolved id matches
            # an already-built card under a different slug. It re-mints a fresh
            # title-slug each tick, so a TTL cool would let it resurrect; escort
            # it PERMANENTLY out of the paced budget (same as DECONTAM_MARK — the
            # card exists, so this proposal slug is a structural dead-end).
            summary['duplicate_identity'] += 1
            event['dup_of'] = outcome.get('dup_of')
            if attempts_ledger_path:
                ledger[slug] = DECONTAM_MARK
        else:
            # Unknown outcome — defensive; count as error, don't crash the batch.
            summary['errors'] += 1
            summary['error_slugs'].append(slug)
            event['outcome'] = f'unknown:{kind}'
            if attempts_ledger_path:
                ledger[slug] = now_iso

        if on_event:
            on_event(event)

    if attempts_ledger_path:
        _save_ledger(ledger, attempts_ledger_path)
    return summary


def _build_arg_parser():
    p = argparse.ArgumentParser(
        description="Resolve-pass: comparator proposals -> work_queue (factory input).")
    p.add_argument('proposals', help="Path to proposals.json (from the comparator miner).")
    p.add_argument('--floor', type=float, default=None,
                   help="Override resolver confidence floor (default: resolver's).")
    p.add_argument('--ollama-url', default=resolve_sku.DEFAULT_OLLAMA_URL,
                   help="Ollama URL for the live Gemma disambiguator.")
    p.add_argument('--model', default=resolve_sku.DEFAULT_MODEL,
                   help="Gemma model tag for disambiguation.")
    p.add_argument('--dry-run', action='store_true',
                   help="Load + report proposals without resolving (no eBay/Gemma).")
    p.add_argument('--max', type=int, default=None, dest='max_new',
                   help="Resolve at most N NEW proposals this run (skip slugs "
                        "already in the work_queue first). Paces the LLM load for "
                        "a frequent cron; omit for the legacy resolve-everything run.")
    p.add_argument('--attempts-ledger', default=ATTEMPTS_LEDGER_PATH, dest='attempts_ledger',
                   help="Ledger that retires recently-attempted, non-productive "
                        "slugs (no_candidate/queued/error) from the paced --max "
                        "budget so the tail resolves instead of the head "
                        "re-blocking. Default: data/resolve-attempts.json.")
    p.add_argument('--no-ledger', action='store_true',
                   help="Disable the attempt ledger (legacy: retry every slug each run).")
    p.add_argument('--retry-ttl-days', type=int, default=RETRY_TTL_DAYS, dest='retry_ttl_days',
                   help=f"Days before a ledgered non-productive slug is retried "
                        f"(default {RETRY_TTL_DAYS}).")
    return p


def main(argv=None):
    args = _build_arg_parser().parse_args(argv)

    try:
        proposals = load_proposals(args.proposals)
    except (OSError, ValueError) as e:
        print(f"[resolve_pass] ERROR loading proposals: {e}", file=sys.stderr)
        return 2

    print(f"[resolve_pass] loaded {len(proposals)} proposal(s) "
          f"(top: {', '.join(p['slug'] for p in proposals[:5])})")

    if args.dry_run:
        print("[resolve_pass] --dry-run: not resolving.")
        return 0

    # Bootstrap the gateway/.env BEFORE importing ebay_api. ebay_api reads
    # EBAY_APP_ID / EBAY_CERT_ID from os.environ at MODULE-LEVEL, so the env must
    # be populated before its import line executes. Under the gateway service
    # app_production does this; but the cron entry point (this main) is invoked
    # by a plain crontab line that inherits a minimal env — nothing would load
    # the secrets file, ebay_api would read empty creds, and every resolve would
    # fail "not configured." This call closes that gap. It runs AFTER the
    # --dry-run return so dry-run / tests stay creds-free (the lazy-import
    # discipline below is preserved). Fallback-only: a real shell export or
    # systemd EnvironmentFile still wins (load_dotenv never overrides).
    import env_bootstrap
    env_bootstrap.load_dotenv()

    # Live collaborators (box-only). Imported lazily so --dry-run and tests don't
    # require eBay creds / a live ollama just to load the module.
    import ebay_api
    import demand_log
    import review_queue

    gemma = resolve_sku.GemmaDisambiguator(
        model=args.model, ollama_url=args.ollama_url)

    def _log(ev):
        tag = ev.get('outcome', '?')
        print(f"  [{tag:>13}] {ev['slug']} (fork={ev.get('fork_n')})")

    summary = run(
        proposals, ebay=ebay_api, gemma=gemma,
        demand_log=demand_log, review_queue=review_queue,
        floor=args.floor, on_event=_log, max_new=args.max_new,
        attempts_ledger_path=(None if args.no_ledger else args.attempts_ledger),
        retry_ttl_days=args.retry_ttl_days,
    )

    print(f"\n[resolve_pass] done: {summary['enrolled']} enrolled, "
          f"{summary['already_queued']} queued (straggler), "
          f"{summary['no_candidate']} no-candidate, {summary['errors']} error(s), "
          f"{summary['skipped_enrolled']} already-queued (skipped), "
          f"{summary['skipped_cooldown']} cooling (skipped), "
          f"{summary['decontaminated']} decontaminated (built dup), "
          f"{summary['duplicate_identity']} duplicate-identity (dropped/flagged), "
          f"{summary['deferred']} deferred (over --max).")
    if summary['enrolled']:
        print(f"[resolve_pass] enrolled -> work_queue (factory will build): "
              f"{', '.join(summary['enrolled_slugs'])}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
