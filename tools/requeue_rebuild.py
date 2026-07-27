#!/usr/bin/env python3
"""Re-open live cards for rebuild after a pipeline gate changed under them.

work_queue.requeue_promoted() had no operator surface — it was a library
function whose only callers were tests. This is that surface, and it exists
because the situation it was written for has recurred: on 2026-07-18 it was
the relevance gate, and on 2026-07-27 it is the dictionary category.

WHAT HAPPENED

build_card threaded --category to enrich and to assemble but not to extract,
the one stage that chooses the vocabulary. classifier_extract kept its
import-time AXIS_TERMS = load_axes("lens/prime"), so every card built through
the driver was extracted as a prime lens regardless of what it was.

Seven camera bodies went live with lens axes — coma, bokeh, vignetting,
filter thread — while the sensor, EVF, burst and battery discussion their
reviewers produced had no axis to land on and was dropped. sony-a7r has no
sensor axis at all, on a camera whose entire identity is its sensor.

WHY RELABELLING CANNOT FIX IT

Looking those axes up in the body dictionary misses, because a lens axis has
no body label — there is no correct name for "optical performance" on a
camera body. The absence is the system telling the truth. The axes are wrong,
not their names, and only re-extraction produces the right ones.

WHY THIS IS CHEAP

resume_stage='extract' rebuilds from the CACHED triples already in the spool:
zero fetch spend, same evidence pool, current gates. That is exactly the shape
wanted here — not new sources, the same sources read with the right
vocabulary. So this is honest about freshness too: last_built moves because
synthesis genuinely is recomputed, and every axis on the card changes.

SAFETY

The live card keeps serving. requeue_promoted moves only the queue record;
the rebuilt card replaces it at /admin/publish behind the usual human gate,
and a rebuild that exhausts its attempts parks `failed` with the live card
still up. Dry-run is the default: --apply is required to write.

    python3 tools/requeue_rebuild.py --bodies-2026-07-27
    python3 tools/requeue_rebuild.py --bodies-2026-07-27 --apply
    python3 tools/requeue_rebuild.py --slug sony-a7r --slug canon-r5 --apply
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'gateway'))

import card_factory  # noqa: E402
import work_queue  # noqa: E402


# The seven bodies extracted with the lens dictionary. sony-a7iv is
# deliberately ABSENT: it was built with the body dictionary and is correct.
# Its three improvable labels are a relabel, not a rebuild — rebuilding a
# sound card to improve a heading would move last_built for work that
# re-analyzed nothing, which is the freshness overclaim ruled out on
# 2026-07-27. sigma and the two tripods are likewise correct as built.
BODIES_2026_07_27 = (
    'canon-r5',
    'canon-r6',
    'sony-a1',
    'sony-a7-v',
    'sony-a7c',
    'sony-a7r',
    'sony-a7s-iii',
)


# The factory's build root. The live crontab runs card_factory with
# `--out /var/lib/askmaddi-cards`, so that is where factory builds keep their
# spool — corpus, checkpoint and cached triples.
#
# NOT derived from build_card's location, which is what this tool did first
# and got wrong for every card. That derivation (card_factory's own, when its
# out_root is None) points at aggregator-build/out/, which holds only the four
# HAND-built cards — and those four are precisely the four that came out
# correct, because a hand build passes --category explicitly while the factory
# never told extract anything. Deriving a runtime path from a source-tree
# location found the wrong four and reported the right seven as missing.
DEFAULT_BUILD_ROOT = Path('/var/lib/askmaddi-cards')

_build_root = DEFAULT_BUILD_ROOT


def triples_dir(slug):
    """Where the cached triples for `slug` live in the factory spool."""
    return _build_root / slug / 'triples'


def inspect(slug, path):
    """Report whether `slug` can be requeued, without changing anything.

    Two preconditions, both documented on requeue_promoted: the record must
    be `promoted` (it refuses anything else), and the cached triples must
    exist, because absent triples fail loudly at extract's _require and burn
    an attempt for nothing.
    """
    queue = work_queue.load_queue(path)
    record = (queue.get('queue') or {}).get(slug)
    if record is None:
        return False, 'no work-queue record'
    state = record.get('state')
    if state != 'promoted':
        return False, f'state is {state!r}, not promoted'
    tdir = triples_dir(slug)
    if not tdir.is_dir():
        return False, f'no cached triples at {tdir} (would burn an attempt)'
    return True, f'promoted, triples cached at {tdir}'


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--slug', action='append', default=[],
                   help='Card slug to requeue. Repeatable.')
    p.add_argument('--bodies-2026-07-27', action='store_true',
                   help='The seven bodies extracted with the lens dictionary.')
    p.add_argument('--apply', action='store_true',
                   help='Actually requeue. Without it this only reports.')
    p.add_argument('--queue-path', default=None,
                   help='Override the work-queue path (testing).')
    p.add_argument('--build-root', default=None,
                   help=f'Factory build root holding the spool. Default '
                        f'{DEFAULT_BUILD_ROOT}, matching the crontab\'s '
                        f'--out. Override only if the factory is run with a '
                        f'different --out.')
    args = p.parse_args(argv)

    global _build_root
    _build_root = Path(args.build_root) if args.build_root else DEFAULT_BUILD_ROOT

    slugs = list(args.slug)
    if args.bodies_2026_07_27:
        slugs = list(BODIES_2026_07_27) + slugs
    if not slugs:
        p.error('nothing to do: pass --slug or --bodies-2026-07-27')

    path = Path(args.queue_path) if args.queue_path else work_queue.WORK_QUEUE_PATH

    print(f"queue: {path}")
    print(f"spool: {_build_root}")
    print(f"mode : {'APPLY' if args.apply else 'dry-run (no writes)'}\n")

    ready, blocked = [], []
    for slug in slugs:
        ok, why = inspect(slug, path)
        print(f"  {'OK  ' if ok else 'SKIP'} {slug:24} {why}")
        (ready if ok else blocked).append(slug)

    print(f"\n{len(ready)} requeueable, {len(blocked)} blocked.")
    if not args.apply:
        print("Dry run — nothing written. Re-run with --apply.")
        return 0
    if not ready:
        print("Nothing to apply.")
        return 1

    print()
    for slug in ready:
        rec = work_queue.requeue_promoted(slug, resume_stage='extract',
                                          path=path)
        print(f"  requeued {slug:24} state={rec['state']} "
              f"resume={rec.get('resume_stage')} from={rec.get('requeued_from')}")

    print(f"\n{len(ready)} card(s) re-opened. The drip claims them at "
          f"{card_factory.DEFAULT_DAILY_CAP}/day, requeued records first "
          f"(claim_next is FIFO on enrolled_at, so veterans outrank new "
          f"mints). Live cards keep serving until /admin/publish approves "
          f"each rebuild.")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
