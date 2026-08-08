#!/usr/bin/env python3
"""
enroll_handbuilt.py — reconcile the hand-curated hand-built-card roster into work_queue.
================================================================================
Some SKUs (tools/handbuilt_roster.json) were authored directly, bypassing
resolve_pass, so they never got a work_queue record and are invisible to the
drip/rebuild machinery. This tool enrolls whichever roster slugs are missing
from the queue, at state 'resolved', via work_queue.enroll (idempotent,
forward-only — never re-enrolls or mutates a slug already present at any
state).

DRY-RUN by default: computes and reports the enrolled/skipped split without
writing anything. Pass --commit to actually enroll the missing slugs.

Usage:
  python3 tools/enroll_handbuilt.py             # dry-run
  python3 tools/enroll_handbuilt.py --commit     # enroll missing roster slugs
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'gateway'))

import work_queue  # noqa: E402


def load_roster(roster_path):
    """Read the roster JSON and return its list of {'slug','label','category'} dicts."""
    data = json.loads(Path(roster_path).read_text(encoding='utf-8'))
    return list(data['roster'])


def enroll_missing(roster_path, queue_path=None, *, commit=False):
    """Enroll roster slugs missing from the queue; dry-run unless commit=True.

    Returns {'enrolled': [slug,...], 'skipped': [slug,...], 'committed': bool}.
    A roster slug already present in the queue (any state) is skipped and left
    untouched. commit=False computes the split but writes nothing.
    """
    roster = load_roster(roster_path)

    if queue_path is None:
        queue = work_queue.load_queue()
    else:
        queue = work_queue.load_queue(queue_path)
    known = set(queue.get('queue', {}))

    enrolled = []
    skipped = []
    for entry in roster:
        slug = entry['slug']
        if slug in known:
            skipped.append(slug)
            continue
        enrolled.append(slug)
        if commit:
            if queue_path is None:
                work_queue.enroll(slug, entry['label'], entry['category'])
            else:
                work_queue.enroll(slug, entry['label'], entry['category'], path=queue_path)

    return {'enrolled': enrolled, 'skipped': skipped, 'committed': bool(commit)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--roster', default=str(ROOT / 'tools' / 'handbuilt_roster.json'),
                    help='path to the hand-built roster JSON')
    ap.add_argument('--queue-path', default=None,
                    help='override the work_queue.json path (default: work_queue\'s own default)')
    ap.add_argument('--commit', action='store_true',
                    help='write missing roster slugs into the queue (default: dry-run)')
    args = ap.parse_args(argv)

    summary = enroll_missing(args.roster, args.queue_path, commit=args.commit)

    mode = 'COMMIT' if summary['committed'] else 'DRY-RUN'
    print(f"[enroll_handbuilt] {mode}: enrolled={summary['enrolled']} skipped={summary['skipped']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
