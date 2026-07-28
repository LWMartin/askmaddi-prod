"""
card_factory.py — the card-factory 24/7 capped drip loop.
================================================================================
Demand-factory Piece 3 (card-factory-design-scope, 2026-06-29). The long-running
consumer that drains the work_queue one card at a time, under a daily cap, 24/7 —
a "stutter rotational build," NOT a batch slam. Each tick:

    cap_remaining(cap) > 0 ?
        no  -> sleep (cap reached, or nothing waiting)
        yes -> claim_next()                              # oldest resolved -> building
               build_card.py --stop-stage assemble       # fetch→extract→enrich→assemble
                 exit 0    -> mark_review_ready()         # -> review_ready, built_today++
                 exit != 0 -> mark_failed_or_retry()      # retry to resolved, or park failed
               sleep

WHY STOP AT ASSEMBLE (the human-in-the-loop seam, 2026-06-29):
  build_card.py's render stage runs build_site.py, which publishes a card into the
  LIVE browser/ surface. That is precisely the step Lee approves by hand. So the
  factory automates everything UP TO render: it builds the card (fetch→...→assemble,
  schema-validated) and stops. The assembled card.json waits at out/<sku>/card.json
  for the /admin publish gate (Piece 4) to render it live. The factory never
  publishes; render is the one in-the-loop touch point.

WHY THE DAILY CAP:
  Self-throttling (no backlog slam) AND a natural rate-limit on the upstream
  fetch/eBay calls. The cap is a per-UTC-day budget tracked in the work_queue
  store (built_today, rolling at midnight); only a CLEAN build counts against it
  (a failed build that retries does not burn cap).

INJECTED RUNNER (offline-testable, live on the box):
  build_card.py's enrich stage needs the VPS gemma (or --enrich-client mock), so
  the real drip runs on the box. tick() takes an injected `runner` callable so the
  routing/cap/state logic is unit-tested offline with a fake runner; production
  passes the real subprocess runner (build_card.py). Same discipline as the
  resolver's injected ebay/gemma and the comparator typer's injected client.

This module performs NO spine writes and NO live publishes. It only advances
work_queue state and shells out to build_card.py (which, stopped at assemble,
writes only to its own out/ build root). The single human gate downstream is
untouched.
"""
import argparse
import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

import work_queue


# Defaults — all tunable via CLI / kwargs. The cap and sleep are deliberately
# conservative for v1; the point is a steady trickle, not throughput.
# Mirror of build_card.EXIT_CORPUS_THIN (build_card lives in the phantom-ops
# workspace — a cross-repo import would couple deploys; the exit code IS the
# contract, same as rc==0). If build_card ever renumbers, both sides change
# together via the runbook.
EXIT_CORPUS_THIN = 3
# Mirror of build_card.EXIT_ENRICH_PARTIAL: the enrich stage checkpointed a
# big corpus and exited asking to be resumed. Neither defect (3-strike) nor
# weather (turtle): the factory's missing verb was "still working"
# (2026-07-17 — sony-a7-v parked 3/3 twice for having too many reviews).
EXIT_ENRICH_PARTIAL = 6
# Mirror of build_card.EXIT_CATEGORY_UNRESOLVED: the SKU has no authored
# `facet`, so no dictionary category could be resolved and build_card refused
# to guess rather than extract the product as a prime lens. Deterministic like
# CORPUS_THIN, so it parks; unlike CORPUS_THIN what re-opens it is a human
# authoring the facet, not better sourcing.
EXIT_CATEGORY_UNRESOLVED = 7

DEFAULT_DAILY_CAP = 4           # cards/day. History: 12 until 2026-07-07, then
                                # 2 (quality-first reframe), then 4 on
                                # 2026-07-27. The cap IS the politeness budget,
                                # not a throughput target — the drip profile
                                # spends ~20 YT fetch attempts per card (see
                                # build_card's PROFILES), so this is ~80
                                # jittered fetches SPREAD ACROSS A DAY against
                                # the bulk era's 50 in one sitting.
                                #
                                # Why 4 and not 6+: compute is NOT the binding
                                # constraint (the 2026-07-19 flock makes
                                # overlapping builds structurally impossible
                                # and enrich serializes, so even 6/day would
                                # occupy ~4.5h), so the only thing this number
                                # protects is upstream exposure. 4 doubles
                                # new-card throughput toward the breadth gate
                                # while leaving headroom to read the drip log
                                # before going further. Raise deliberately,
                                # with the log in hand — not because a build
                                # slot looked idle.
DEFAULT_TICK_SLEEP = 300        # seconds between ticks (5 min) when work remains
DEFAULT_IDLE_SLEEP = 1800       # seconds to sleep when capped or queue-empty (30 min)

# build_card.py lives in the phantom-ops workspace, not askmaddi-prod. The factory
# runs on the box where both repos are checked out; this path is overridable so the
# sandbox/tests never depend on it (the injected runner bypasses it entirely).
DEFAULT_BUILD_CARD = (
    Path.home() / 'phantom-ops' / 'claude' / 'workspace'
    / 'aggregator-build' / 'build_card.py'
)

def _log_line(msg):
    """Print a drip-log line prefixed with a UTC timestamp.

    WHY (papercut, three sessions running — 2026-07-19/20 morning verifies):
    drip.log lines carried no timestamps, so 30 idle ticks were undatable and
    the midnight-collision forensics had to be reconstructed from work_queue
    timestamps instead of the log itself. Every line the cron appends to
    drip.log now self-dates. Format matches work_queue's _now() (ISO-8601 Z,
    second precision) so log lines and queue records sort together.
    """
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}")


# Single-flight lock (2026-07-19). Lives beside the work_queue store — it guards
# the same resource (one factory invocation at a time). MUST stay in .gitignore's
# runtime family: an untracked file in the checkout aborts the nightly banker's
# clean-tree preflight (the 2026-06-25 / 2026-06-30 failure class).
DEFAULT_LOCK_PATH = work_queue.WORK_QUEUE_PATH.parent / '.factory.lock'


def single_flight(lock_path=DEFAULT_LOCK_PATH):
    """Try to become the only running factory invocation. Returns an open fd
    (keep it for the process lifetime) or None if another invocation holds it.

    WHY (incident 2026-07-18→19, sony-a7c): the drip cron fires every 15 min
    with no mutual exclusion. A tick whose build_card is still running (enrich
    on a big corpus takes ~45 min) overlaps the next tick; at the midnight cap
    reset TWO fresh builds (canon-r5/r6) piled onto a running a7c enrich, all
    three contending for the one CPU-bound gemma shim. Per-claim latency
    climbed 8s -> 17s -> 24s -> socket.timeout: the enrich died at 304/567,
    the assembled card never refreshed, and the contaminated card stayed live
    an extra day. The gate makes overlap structurally impossible instead of
    latency-dependent.

    WHY flock, not a PID/TTL lockfile: flock(2) is released by the kernel when
    the process exits — cleanly, on crash, or on SIGKILL — so there is no stale
    lock to reap and no TTL to tune. A skipped tick costs 15 minutes; a wrongly
    honored stale lock could silence the factory indefinitely. LOCK_NB because
    cron mode must never queue behind a running build (the next tick will come).

    WHY here and not in tick(): tick() is pure state-machine orchestration over
    an injected runner (offline-testable, no OS coupling). The overlap being
    prevented is between PROCESSES, so the process entrypoint owns the lock.
    """
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o664)
    try:
        # Mirror work_queue._atomic_write's fchmod: O_CREAT mode is filtered by
        # the umask, and the pipeline group needs rw on shared runtime files.
        os.fchmod(fd, 0o664)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def build_card_runner(build_card_path=DEFAULT_BUILD_CARD, askmaddi_prod=None,
                      enrich_client='flask', python=None, out_root=None,
                      yt=False):
    """Produce the PRODUCTION runner: a callable(record) -> (rc, card_path, detail).

    The returned callable shells out to build_card.py --stop-stage assemble for one
    work_queue record, mapping the record's fields onto build_card's CLI:
        slug      -> --sku-id
        label     -> --sku-label
        category  -> --category
        seed_urls -> --seed-urls   (when present)
        aliases   -> --alias ...   (repeated)
        mount     -> --mount       (when present)

    `out_root` is the cross-user seam (decision 2026-06-30, factory-drip
    handoff): the factory runs as phantomops (owner of build_card + deps) but
    the /admin gate reads as askmaddi, so the assembled card must land in the
    neutral group-shared spool `/var/lib/askmaddi-cards` — never either user's
    home. When out_root is set, each build gets `--out <out_root>/<slug>` and
    card_path is derived from the SAME root the subprocess writes to (the
    record's card_path is what /admin previews and publishes — divergence here
    is an invisibly empty gate). When out_root is None the historical default
    holds — build_card's own HERE.parent.parent/out/<sku> — but it is now
    passed to the subprocess explicitly rather than predicted, so the two
    cannot disagree. Before 2026-07-28 this branch computed a DIFFERENT root
    (out/ beside the script, two levels off) and reported it without passing
    it: the build landed in one place and /admin previewed another, which for
    a slug with an old hand build there meant previewing a stale card.

    Returns (returncode, card_path, detail):
      returncode  build_card.py's exit code (0 clean, non-zero failure)
      card_path   <root>/<sku>/card.json the assembled card landed at (for /admin)
      detail      a short human string (stderr tail on failure, 'ok' on success)

    The runner is a CLOSURE over the box-specific config so tick() stays pure.
    """
    python = python or sys.executable
    build_card_path = Path(build_card_path)

    def _run(record):
        slug = record['slug']
        if out_root is not None:
            build_root = Path(out_root) / slug
        else:
            # build_card's own default, which is HERE.parent.parent/"out"/<sku>
            # with HERE = the directory holding build_card.py -- NOT
            # <that directory>/out, which is what this line used to compute.
            # Those differ by two levels, and the wrong one is worse than a
            # missing path: aggregator-build/out/ EXISTS and holds hand builds,
            # so a run without --out wrote to one place and reported a stale
            # June card at another. /admin previews record['card_path'], which
            # made that an invisibly WRONG gate, not merely an empty one.
            build_root = Path(build_card_path).resolve().parent.parent.parent \
                / 'out' / slug
        card_path = build_root / 'card.json'

        cmd = [
            python, str(build_card_path),
            '--sku-id', slug,
            '--sku-label', record['label'],
            '--category', record.get('category') or 'lens',
            '--stop-stage', 'assemble',
            '--enrich-client', enrich_client,
            # ALWAYS passed, including on the default path. Predicting where
            # the subprocess will write is a behavioral promise -- it holds
            # only while two formulas in two repos agree, and it already
            # failed once. Dictating the root is structural: build_card's own
            # default is never consulted, so it cannot drift out from under
            # us. The formula above is now a starting value, not a prediction.
            '--out', str(build_root),
        ]
        if record.get('resume_stage'):
            # A prior tick exited ENRICH_PARTIAL: the corpus + fingerprinted
            # checkpoint are waiting in the spool. Starting from fetch would
            # rebuild the corpus and orphan the checkpoint (the 1609/929
            # incident); resume where the work actually is.
            cmd += ['--start-stage', record['resume_stage']]
        if record.get('seed_urls'):
            cmd += ['--seed-urls', record['seed_urls']]
        for alias in (record.get('aliases') or []):
            cmd += ['--alias', alias]
        if record.get('mount'):
            cmd += ['--mount', record['mount']]
        if askmaddi_prod:
            cmd += ['--askmaddi-prod', str(askmaddi_prod)]
            # Spine identity (images-on-spine step 4): pass the spine
            # EXPLICITLY rather than leaning on build_card's prod_root
            # derivation — the factory knows the root, and an explicit argv
            # is what the test contract pins. build_card forwards it to the
            # assemble stage, where spine_identity() maps the SKU's skus.json
            # entry (image pick + subcategory + brand/model) into card
            # identity. Absent entry degrades loudly to pre-spine behavior.
            cmd += ['--spine', str(Path(askmaddi_prod) / 'data' / 'skus.json')]
            # Factory posture (2026-07-17, after the 0600-perms silent
            # degradation): a factory build only exists because the SKU
            # RESOLVED — its spine entry must exist and must be readable.
            # Any spine miss is a stop-the-line infra defect (exit 4,
            # deterministic 3-strike), never a quiet pre-spine card.
            cmd += ['--require-spine']
            # Mint date (2026-07-27): the PUBLISHED card is the birth
            # certificate. Passed explicitly for the same reason as --spine —
            # the factory knows the root and the argv is what the test
            # contract pins. build_card forwards it to assemble, which carries
            # freshness.created_at into the rebuild.
            #
            # Deliberately NOT gated on existence and deliberately NOT fatal
            # when it misses, unlike --require-spine. A card built for the
            # first time has no published counterpart, which is normal, and a
            # rebuild whose published card is unreadable still has the build
            # root to fall back on. assemble reports either fallback on stdout,
            # so the drip log carries the evidence instead of the date quietly
            # resetting — which is the failure this whole wire exists to end.
            cmd += ['--prior-card',
                    str(Path(askmaddi_prod) / 'data' / 'cards' / f'{slug}.json')]
        if yt:
            # Stage 1b: paced YT transcript leg into the same triples dir.
            # Factory-global posture, not per-record — quality-first cards
            # are what the drip exists to produce. Egress follows PROXY_URL
            # (inherited env); unset = bare box IP, the honest default.
            cmd += ['--yt']

        proc = subprocess.run(
            cmd, cwd=str(build_card_path.parent),
            capture_output=True, text=True,
        )
        # Persist the FULL transcript regardless of outcome (2026-07-17,
        # after two incidents the discard hid: the 0600-perms silent
        # pre-spine degradation rode a SUCCESSFUL build's discarded stdout,
        # and a7-v/a7c parked with a [!] warning as their last_error while
        # the real fatal died with the pipe). One file per build root,
        # overwritten in place — bounded by construction.
        _persist_build_log(build_root, cmd, proc)
        if proc.returncode == 0:
            return 0, str(card_path), 'ok'
        return proc.returncode, str(card_path), _failure_detail(proc)

    return _run


def _persist_build_log(build_root, cmd, proc):
    """Write the full build transcript to <build_root>/build.log. Best-effort:
    a log-write failure must never convert a build outcome."""
    try:
        build_root = Path(build_root)
        build_root.mkdir(parents=True, exist_ok=True)
        (build_root / 'build.log').write_text(
            'cmd: ' + ' '.join(cmd) + '\n'
            + f'exit: {proc.returncode}\n'
            + '─── stdout ───\n' + (proc.stdout or '')
            + '\n─── stderr ───\n' + (proc.stderr or '') + '\n')
    except OSError:
        pass


# Lines matching these mark the ACTUAL failure; a tail that happens to end on
# a [!] advisory is how a7-v/a7c parked with a warning as their last_error.
_FATAL_MARKERS = ('Traceback', 'ERROR', 'FATAL', 'error:', 'Error:',
                  'Refusing', 'CRITICAL', 'Exception')


def _failure_detail(proc, budget=480):
    """Compose last_error from BOTH streams, fatal-first.

    Old behavior — `(stderr or stdout).splitlines()[-1]` — dropped stdout
    whenever stderr held so much as one advisory line, and kept one line of
    whatever won. New: scan both streams for fatal-marker lines (taking the
    LAST match — for a traceback that's the exception itself), then append
    each stream's tail for context, inside the work_queue's 500-char cap.
    """
    out = (proc.stdout or '').strip()
    err = (proc.stderr or '').strip()
    combined = (out + '\n' + err).splitlines()

    fatal = ''
    for line in combined:
        s = line.strip()
        if s and any(m in s for m in _FATAL_MARKERS):
            fatal = s
    parts = [f'exit {proc.returncode}']
    if fatal:
        parts.append(fatal)
    for name, stream in (('stderr', err), ('stdout', out)):
        tail = [l.strip() for l in stream.splitlines() if l.strip()][-2:]
        for l in tail:
            if l != fatal and l not in parts:
                parts.append(f'[{name}] {l}')
    detail = ' | '.join(parts)
    return detail[:budget] if detail else f'exit {proc.returncode}'


def tick(runner, *, cap=DEFAULT_DAILY_CAP, path=work_queue.WORK_QUEUE_PATH):
    """Run ONE factory tick. Returns a structured outcome dict.

    Pure orchestration over the work_queue contract + an injected `runner`
    (callable(record) -> (rc, card_path, detail)). Never raises on a build
    failure — a non-zero build is a routing decision (retry/park), not an
    exception; only a genuinely broken runner contract would propagate.

    Outcomes:
      {'action': 'capped',    'remaining': 0}            cap reached for today
      {'action': 'idle',      'remaining': n}            cap left, but queue empty
      {'action': 'built',       'slug': s, 'card_path': p} clean build -> review_ready
      {'action': 'retry',       'slug': s, 'detail': d}    build failed, will retry
      {'action': 'failed',      'slug': s, 'detail': d}    build failed, budget spent
      {'action': 'corpus_thin', 'slug': s, 'detail': d}    floor abstained -> parked
    """
    remaining = work_queue.cap_remaining(cap, path=path)
    if remaining <= 0:
        return {'action': 'capped', 'remaining': 0}

    record = work_queue.claim_next(path=path)
    if record is None:
        return {'action': 'idle', 'remaining': remaining}

    slug = record['slug']
    rc, card_path, detail = runner(record)

    if rc == 0:
        rec = work_queue.mark_review_ready(slug, path=path)
        # Stash where the assembled card landed so /admin can preview it.
        # (mark_review_ready returns the record; persist card_path alongside.)
        _attach_card_path(slug, card_path, path=path)
        return {'action': 'built', 'slug': slug, 'card_path': card_path}

    if rc == EXIT_CORPUS_THIN:
        # build_card's corpus floor abstained — deterministic verdict, so no
        # retry (see mark_corpus_thin). Not counted against the daily cap.
        work_queue.mark_corpus_thin(slug, detail, path=path)
        return {'action': 'corpus_thin', 'slug': slug, 'detail': detail}

    if rc == EXIT_CATEGORY_UNRESOLVED:
        # Refused before stage 1 — no fetch effort was spent. Parks for a
        # human to author the facet; requeue() re-opens it.
        work_queue.mark_needs_category(slug, detail, path=path)
        return {'action': 'needs_category', 'slug': slug, 'detail': detail}

    if rc == EXIT_ENRICH_PARTIAL:
        rec = work_queue.mark_enrich_partial(slug, detail, path=path)
        return {'action': 'enrich_partial', 'slug': slug, 'detail': detail,
                'attempts': rec.get('build_attempts')}

    rec, terminal = work_queue.mark_failed_or_retry(slug, detail, path=path)
    if terminal:
        action = 'failed'
    elif rec.get('cooldown_until'):
        # Transient (turtle path): attempts unburned, record cooling down.
        action = 'cooldown'
    else:
        action = 'retry'
    return {
        'action': action,
        'slug': slug, 'detail': detail,
        'attempts': rec.get('build_attempts'),
        'transient_retries': rec.get('transient_retries', 0),
        'cooldown_until': rec.get('cooldown_until'),
    }


def _attach_card_path(slug, card_path, *, path=work_queue.WORK_QUEUE_PATH):
    """Record the assembled card's path on the work_queue record (for /admin preview).

    A tiny targeted write: the /admin publish surface (Piece 4) renders the preview
    from this path and, on approve, feeds it to build_site.py. Kept as a thin
    helper rather than widening mark_review_ready's signature, so the work_queue
    state machine stays about STATE and the factory owns build artifacts.
    """
    queue = work_queue.load_queue(path)
    rec = queue.get('queue', {}).get(slug)
    if rec is not None:
        rec['card_path'] = card_path
        work_queue._atomic_write(queue, path)


def run_loop(runner, *, cap=DEFAULT_DAILY_CAP, tick_sleep=DEFAULT_TICK_SLEEP,
             idle_sleep=DEFAULT_IDLE_SLEEP, max_ticks=None,
             path=work_queue.WORK_QUEUE_PATH, sleep=time.sleep, log=_log_line):
    """The thin 24/7 wrapper: tick -> sleep -> repeat.

    Sleeps `tick_sleep` after doing work (steady drip), `idle_sleep` when capped or
    idle (back off when there's nothing to do). `max_ticks` bounds the loop for
    tests/cron-style single passes (None = run forever). `sleep` and `log` are
    injected so tests drive the loop deterministically with no real waiting.

    This is form-agnostic by design: run it as a long-lived service, or call
    tick() once from cron — same logic, the loop is just the service wrapper.
    """
    ticks = 0
    while max_ticks is None or ticks < max_ticks:
        outcome = tick(runner, cap=cap, path=path)
        action = outcome['action']
        if action == 'built':
            log(f"[factory] built {outcome['slug']} -> review_ready "
                f"({outcome['card_path']})")
            nap = tick_sleep
        elif action in ('retry', 'failed'):
            log(f"[factory] {action} {outcome['slug']}: {outcome['detail']} "
                f"(attempt {outcome.get('attempts')})")
            nap = tick_sleep
        elif action == 'capped':
            log("[factory] daily cap reached — idling")
            nap = idle_sleep
        else:  # idle
            log("[factory] queue empty — idling")
            nap = idle_sleep

        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        sleep(nap)
    return ticks


def _build_arg_parser():
    p = argparse.ArgumentParser(
        description="AskMaddi card factory — 24/7 capped drip build loop.")
    p.add_argument('--cap', type=int, default=DEFAULT_DAILY_CAP,
                   help=f"Daily card cap (default {DEFAULT_DAILY_CAP}).")
    p.add_argument('--tick-sleep', type=int, default=DEFAULT_TICK_SLEEP,
                   help="Seconds between ticks when working.")
    p.add_argument('--idle-sleep', type=int, default=DEFAULT_IDLE_SLEEP,
                   help="Seconds to sleep when capped/idle.")
    p.add_argument('--once', action='store_true',
                   help="Run a single tick and exit (cron mode).")
    p.add_argument('--build-card', default=str(DEFAULT_BUILD_CARD),
                   help="Path to build_card.py.")
    p.add_argument('--askmaddi-prod', default=None,
                   help="Path to askmaddi-prod (passed to build_card render input).")
    p.add_argument('--out', default=None, dest='out_root',
                   help="Neutral card spool root (box: /var/lib/askmaddi-cards). "
                        "Each build writes <out>/<slug>/card.json; the record's "
                        "card_path points there for the /admin gate. Default: "
                        "build_card's own out/<slug> (sandbox/manual).")
    p.add_argument('--yt', action='store_true',
                   help='Quality-first: pass --yt to build_card so every drip '
                        'build runs Stage 1b (paced YouTube transcript leg). '
                        'Egress follows PROXY_URL from the environment.')
    p.add_argument('--enrich-client', choices=['flask', 'mock'], default='flask',
                   help="enrich backend: flask (VPS gemma) or mock (offline).")
    p.add_argument('--status', action='store_true',
                   help="Print work_queue state histogram and exit.")
    p.add_argument('--lock', default=str(DEFAULT_LOCK_PATH), dest='lock_path',
                   help="Single-flight lock path (flock). A tick that finds it "
                        "held logs {'action': 'locked_out'} and exits 0 — the "
                        "previous invocation's build is still running.")
    return p


def main(argv=None):
    args = _build_arg_parser().parse_args(argv)

    if args.status:
        c = work_queue.counts()
        print(f"[factory] queue: {c}")
        return 0

    # Single-flight gate: held for the entire invocation (claim + build), in
    # both cron (--once) and loop modes. Deliberately NOT applied to --status,
    # which is a read-only peek and must work while a build runs. The fd is
    # kept referenced until process exit; the kernel releases the flock then.
    lock_fd = single_flight(Path(args.lock_path))
    if lock_fd is None:
        _log_line("[factory] tick: {'action': 'locked_out'}")
        return 0

    runner = build_card_runner(
        build_card_path=args.build_card,
        askmaddi_prod=args.askmaddi_prod,
        enrich_client=args.enrich_client,
        out_root=args.out_root,
        yt=args.yt,
    )

    if args.once:
        outcome = tick(runner, cap=args.cap)
        _log_line(f"[factory] tick: {outcome}")
        return 0

    run_loop(runner, cap=args.cap,
             tick_sleep=args.tick_sleep, idle_sleep=args.idle_sleep)
    return 0


if __name__ == '__main__':
    sys.exit(main())
