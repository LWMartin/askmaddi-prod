#!/usr/bin/env python3
"""ledger_clear.py — clear stale resolve-attempts ledger entries so delisted
products can re-mint. READ-ONLY by default (dry-run); --apply writes.

WHY THIS EXISTS (2026-09-03, aerial delist+re-mint)
resolve_pass.py paces retries with data/resolve-attempts.json: a slug is skipped
if its last attempt is within a 7-day TTL (transient 'cooling') OR if it carries
the permanent 'decontaminated' sentinel — a proposal that resolved to a canonical
ALREADY built, escorted out forever so it can't re-block the paced head.

After a delist, that sentinel goes STALE: the canonical the clean proposal used
to collide with is gone, so it should re-mint — but the permanent mark keeps
resolve_pass skipping it, and --retry-ttl-days can't clear a sentinel (only the
timestamp kind). This tool removes the stale ledger keys for the delisted set so
the next resolve pass attempts them fresh.

SELECT the keys to clear by (combine freely; a key matches if ANY selector hits):
  --slugs-file FILE   explicit slugs, one per line (# comments/blanks ignored)
  --pattern REGEX     regex over the slug (default: the aerial vertical)
  --decontaminated-only   restrict to sentinel entries (leave transient cools)

Owner: resolve_pass writes the ledger as the askmaddi user, so run this as
askmaddi (a root write would flip ownership and break the cron's next write).

  sudo -u askmaddi python3 tools/ledger_clear.py                 # dry-run, aerial
  sudo -u askmaddi python3 tools/ledger_clear.py --apply
  sudo -u askmaddi python3 tools/ledger_clear.py --slugs-file /tmp/x.txt --apply
"""
import argparse
import json
import re
from pathlib import Path

DECONTAM_MARK = 'decontaminated'  # mirrors resolve_pass.DECONTAM_MARK
_DEFAULT_AERIAL = (r'dji|mavic|mini|neo|avata|osmo|ronin|inspire|goggles'
                   r'|autel|hoverair|zerozero|skydio|parrot|anafi|evo|drone')


def select(ledger, *, slugs=None, pattern=None, decontaminated_only=False):
    """Return the ledger keys to clear given the selectors. A key qualifies if it
    is in `slugs` OR matches `pattern`; then (optionally) only sentinel entries."""
    slugs = set(slugs or [])
    rx = re.compile(pattern, re.I) if pattern else None
    hits = []
    for k, v in ledger.items():
        if not (k in slugs or (rx and rx.search(k))):
            continue
        if decontaminated_only and v != DECONTAM_MARK:
            continue
        hits.append(k)
    return hits


def main(argv=None):
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument('--ledger-path', default=str(root / 'data' / 'resolve-attempts.json'))
    ap.add_argument('--slugs-file', default=None,
                    help='Explicit slugs to clear, one per line (# comments ok).')
    ap.add_argument('--pattern', default=None,
                    help=f'Regex over slug to clear. Default (if no --slugs-file): '
                         f'the aerial vertical.')
    ap.add_argument('--decontaminated-only', action='store_true',
                    help='Only clear permanent sentinel entries, leave transient cools.')
    ap.add_argument('--apply', action='store_true',
                    help='Actually remove. Without it this only reports (dry-run).')
    args = ap.parse_args(argv)

    path = Path(args.ledger_path)
    ledger = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}

    slugs = []
    if args.slugs_file:
        for line in Path(args.slugs_file).read_text(encoding='utf-8').splitlines():
            s = line.split('#', 1)[0].strip()
            if s:
                slugs.append(s)
    # Default to the aerial pattern only when the caller gave no explicit selector.
    pattern = args.pattern or (None if slugs else _DEFAULT_AERIAL)

    hits = select(ledger, slugs=slugs, pattern=pattern,
                  decontaminated_only=args.decontaminated_only)
    decon = [k for k in hits if ledger.get(k) == DECONTAM_MARK]
    cool = [k for k in hits if ledger.get(k) != DECONTAM_MARK]

    print(f"ledger  : {path}  ({len(ledger)} entries)")
    print(f"selector: slugs={len(slugs)} pattern={pattern!r} "
          f"decontaminated_only={args.decontaminated_only}")
    print(f"matched : {len(hits)}  ({len(decon)} decontaminated · {len(cool)} cooling)\n")
    for k in sorted(hits):
        print(f"  clear  {'[decon]' if ledger.get(k) == DECONTAM_MARK else '[cool] '}  {k}")

    if not args.apply:
        print(f"\nDry run — nothing written. Re-run with --apply to clear {len(hits)}.")
        return 0

    for k in hits:
        ledger.pop(k, None)
    tmp = str(path) + '.tmp'
    Path(tmp).write_text(json.dumps(ledger, indent=2, sort_keys=True), encoding='utf-8')
    Path(tmp).replace(path)
    print(f"\nAPPLIED — removed {len(hits)} ent(y/ies); ledger now {len(ledger)}.")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
