#!/usr/bin/env python3
"""Read-only digest of every card waiting at /admin/publish.

For each work_queue record in state review_ready it prints, in one block:

  - the verify_rebuilt_card verdict (run as a subprocess, exit code kept:
    0 safe, 1 do not approve, 2 could not check);
  - the relevance sidecar written next to the assembled card: sources total,
    kept, dropped, and every dropped title with its reason and the confusable
    names that tripped it;
  - whether the slug is on a rebuild roster in tools/rename_plans/ (so a card
    that arrived through the ordinary drip is labelled as such).

After the review_ready blocks it lists every DELIBERATELY RE-OPENED record
(requeued_at set) that parked `corpus_thin` or `failed` instead. A regather that
parks thin found too few reviews of its own product; the 2026-11-18 ruling is
to delist it rather than keep serving the old card, so those slugs are printed
as delist_card dry-run lines. The ruling is about a CARD: a record is a card
when its live card file exists in --cards-dir, or when it was re-opened from
`promoted` (requeue_promoted stamps requeued_from='promoted'; the file may
already be gone while its spine entry is not). requeued_from alone is not
enough: a SECOND re-open of the same record (regather_card on a record already
parked corpus_thin goes through requeue(), which stamps requeued_from=
'corpus_thin') erases the 'promoted' mark while the live page keeps serving
(2026-11-23: six held-identity cards parked thin on cached triples and needed
a regather). A thin record with neither was never a card; delisting it
retires a mint and stops serving nothing, and zero corpus there usually means a
fetch lane came back empty, not that the product is unreviewed (2026-11-19,
om-system-20mm-f1-4: both legs empty). Those are listed as left parked;
corpus_thin_recovery.py --refetch is their door. `failed` is listed for
diagnosis only: a crash says nothing about the product. Records that parked
thin without ever being re-opened are old mints, never cards, and are not this
digest's business.

The delist is printed as delist_card's own two modes: --files-only in the work
clone (the tracked card files, committed and landed by git pull) and
--spine-only on the box as askmaddi (data/skus.json, live-written). One
combined run on the box would delete tracked files out from under the
checkout's git pull.

It writes nothing. It exists so a batch of rebuilds can be reviewed from one
paste instead of one command per card, and so the dropped titles can be held
against the gate simulation that predicted them (2026-11-17 registry v1.10.0).

    python3 tools/review_rebuilds.py
    python3 tools/review_rebuilds.py --queue data/work_queue.json --cards-dir data/cards
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def roster_slugs(plans_dir):
    out = {}
    for p in sorted(Path(plans_dir).glob('*gated-rebuild*.json')):
        try:
            for s in json.loads(p.read_text(encoding='utf-8')).get('slugs', []):
                out.setdefault(s, p.name)
        except (OSError, ValueError):
            continue
    return out


def verify(card_path, published):
    cmd = [sys.executable, str(HERE / 'verify_rebuilt_card.py'), str(card_path)]
    if published.is_file():
        cmd += ['--published', str(published)]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True)
    keep = [ln.rstrip() for ln in r.stdout.splitlines()
            if ln.strip().startswith(('WARN', 'FAIL', 'VERDICT', 'CANNOT'))]
    return r.returncode, keep


def sidecar_lines(card_path, width):
    sc = Path(card_path).with_name('relevance_sidecar.json')
    if not sc.is_file():
        return ['  sidecar: none (no source was flagged, or the gate did not run)']
    d = json.loads(sc.read_text(encoding='utf-8'))
    out = [f"  sidecar: {d.get('mode')}  total {d.get('total_sources')}  "
           f"kept {d.get('kept')}  dropped {d.get('dropped')}  "
           f"reasons {d.get('reason_counts')}"]
    for r in d.get('near_miss', []):
        hits = ','.join(r.get('confusable_hits') or []) or '-'
        title = (r.get('title') or '')[:width]
        out.append(f"    {r.get('decision', '?')[:4]} {r.get('reason')} [{hits}] {title}")
    return out


def parked_regathers(q):
    """(thin, failed): re-opened records whose rebuild parked instead of
    reaching review. Sorted slugs; each item is (slug, record)."""
    thin, failed = [], []
    for slug in sorted(q):
        rec = q[slug]
        if not rec.get('requeued_at'):
            continue
        if rec.get('state') == 'corpus_thin':
            thin.append((slug, rec))
        elif rec.get('state') == 'failed':
            failed.append((slug, rec))
    return thin, failed


def parked_lines(q, cards_dir, width):
    thin, failed = parked_regathers(q)
    out = [f'{len(thin)} re-opened card(s) parked corpus_thin, '
           f'{len(failed)} failed']
    for label, rows in (('thin', thin), ('failed', failed)):
        for slug, rec in rows:
            live = 'live card up' if (Path(cards_dir) / f'{slug}.json').is_file() else 'no live card'
            out.append(f'  {label:6} {slug}  [{live}; from {rec.get("requeued_from") or "?"}]  '
                       f'{(rec.get("last_error") or "")[:width]}')
    def is_card(slug, rec):
        return ((Path(cards_dir) / f'{slug}.json').is_file()
                or rec.get('requeued_from') == 'promoted')
    was_card = [s for s, rec in thin if is_card(s, rec)]
    never_card = [s for s, rec in thin if not is_card(s, rec)]
    if was_card:
        slugs = ' '.join(f'--slug {s}' for s in was_card)
        out.append('  delist (dry runs; add --apply to each to write):')
        out.append('    work clone: python3 tools/delist_card.py --files-only '
                   + slugs)
        out.append('    box:        sudo -u askmaddi python3 tools/delist_card.py '
                   '--spine-only ' + slugs)
    if never_card:
        out.append('  left parked, never a card (not delisted; '
                   'corpus_thin_recovery.py --refetch is the door): '
                   + ' '.join(never_card))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--queue', default='data/work_queue.json')
    ap.add_argument('--cards-dir', default='data/cards')
    ap.add_argument('--plans-dir', default=str(HERE / 'rename_plans'))
    ap.add_argument('--width', type=int, default=90)
    args = ap.parse_args(argv)

    q = json.loads(Path(args.queue).read_text(encoding='utf-8')).get('queue', {})
    ready = sorted(s for s, r in q.items() if r.get('state') == 'review_ready')
    roster = roster_slugs(args.plans_dir)
    tally = {0: 0, 1: 0, 2: 0}
    print(f'{len(ready)} card(s) review_ready\n')
    for slug in ready:
        rec = q[slug]
        card_path = Path(rec.get('card_path') or '')
        origin = f"roster {roster[slug]}" if slug in roster else 'drip (not on a roster)'
        print(f'== {slug}  [{origin}]')
        if not card_path.is_file():
            print(f'  card.json missing at {card_path}\n')
            tally[2] += 1
            continue
        code, lines = verify(card_path, Path(args.cards_dir) / f'{slug}.json')
        tally[code if code in tally else 2] += 1
        for ln in lines:
            print(f'  {ln.strip()}')
        for ln in sidecar_lines(card_path, args.width):
            print(ln)
        print()
    print(f'summary: {tally[0]} safe, {tally[1]} do not approve, {tally[2]} could not check')
    print()
    for ln in parked_lines(q, args.cards_dir, args.width):
        print(ln)
    return 0


if __name__ == '__main__':
    sys.exit(main())
