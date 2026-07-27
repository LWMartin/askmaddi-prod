#!/usr/bin/env python3
"""
Back-fill freshness.created_at from git history — one-time mint-date repair.
================================================================================
WHY THIS EXISTS
---------------
Until 2026-07-27 the aggregator's two assemblers (phantom-ops
claude/workspace/aggregator-build/assemble_card.py) stamped created_at,
last_built and last_checked with the same build-time `now`, and nothing read
the prior card. Every rebuild therefore RESET the card's mint date. Measured
the day the bug was found: 11 of 11 published cards had lost their birth date,
by 1 to 18 days.

The writer is fixed (`--prior-card`, resolve_prior_created_at). That fix
PRESERVES whatever created_at it finds — which means running the rebuild before
this repair would freeze the wrong dates permanently. Repair first, then
rebuild.

WHERE THE TRUTH COMES FROM
--------------------------
Two independent witnesses in git, per card file:

  A. the earliest created_at value any historical revision of the file ever
     carried — the build date as recorded at the time, and
  B. the author date of the commit that first added the file — the publish
     date, which cannot precede the build.

The recovered mint is min(A, B). Build precedes publish, so the earlier of the
two witnesses is the honest floor. Where a revision predates the freshness
block entirely, A is simply absent and B stands alone.

THE SAFETY INVARIANT
--------------------
created_at only ever moves BACKWARD. A candidate later than the stored value is
refused, loudly, and never written — there is no legitimate reason for a card's
birth certificate to get younger, so a forward move means the witnesses are
wrong and a human should look.

SCOPE
-----
Touches data/cards/*.json only. created_at appears in NO served artifact —
verified 2026-07-27: it is absent from browser/ entirely and absent from
cards-manifest.json, whose per-card keys carry no freshness block. build_site.py
reads last_built and computes staleness at render. So this repair needs no site
rebuild and changes no rendered byte.

USAGE
  python3 tools/backfill_created_at.py                  # dry-run table (default)
  python3 tools/backfill_created_at.py --card sony-a7iv # one card
  python3 tools/backfill_created_at.py --commit         # write the repairs
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CARDS_DIR = ROOT / 'data' / 'cards'

EXIT_OK = 0
EXIT_REFUSED = 2        # a candidate would have moved a mint date forward
EXIT_UNUSABLE_REPO = 3  # shallow clone / not a git repo — witnesses unavailable


# ---------------------------------------------------------------------------
# git witnesses
# ---------------------------------------------------------------------------

def _git(*args, repo=ROOT):
    out = subprocess.run(('git', '-C', str(repo)) + args,
                         capture_output=True, text=True)
    return out.stdout


def repo_is_usable(repo=ROOT):
    """A shallow clone silently hides the oldest revisions — which are exactly
    the ones holding the true mint dates. Refusing is mandatory: a shallow repo
    yields plausible-looking but too-recent answers, i.e. the same class of
    quiet wrongness this tool exists to undo."""
    if not (Path(repo) / '.git').exists():
        return False, 'not a git repository'
    if _git('rev-parse', '--is-shallow-repository', repo=repo).strip() == 'true':
        return False, 'shallow clone — oldest revisions absent'
    return True, 'ok'


def revisions(rel_path, repo=ROOT):
    """(sha, author_date_iso) for every revision of the file, newest first."""
    lines = _git('log', '--follow', '--format=%H %aI', '--', str(rel_path),
                 repo=repo).splitlines()
    return [tuple(line.split(None, 1)) for line in lines if ' ' in line]


def earliest_recorded_created_at(rel_path, repo=ROOT):
    """Witness A — the oldest created_at any revision of this file recorded.

    Walks from the oldest revision forward and returns the first parseable
    value found. Revisions that predate the freshness block, or that cannot be
    parsed at all, are skipped rather than fatal: a partial witness set is
    still useful, and this is a recovery path."""
    for sha, _date in reversed(revisions(rel_path, repo=repo)):
        blob = _git('show', f'{sha}:{rel_path}', repo=repo)
        if not blob.strip():
            continue
        try:
            card = json.loads(blob)
        except ValueError:
            continue
        value = (card.get('freshness') or {}).get('created_at')
        if isinstance(value, str) and value.strip():
            return value, sha
    return None, None


def first_add_date(rel_path, repo=ROOT):
    """Witness B — author date of the commit that first added the file."""
    revs = revisions(rel_path, repo=repo)
    return (revs[-1][1], revs[-1][0]) if revs else (None, None)


# ---------------------------------------------------------------------------
# recovery
# ---------------------------------------------------------------------------

def _as_dt(value):
    """Parse an ISO-8601 timestamp to an aware datetime, or None.

    Comparison MUST go through this. git's %aI emits the committer's LOCAL
    offset while the assembler writes UTC, so two timestamps can sort one way
    as strings and the opposite way in time: 2026-06-22T23:00-06:00 is
    chronologically LATER than 2026-06-23T00:30+00:00 despite sorting earlier.
    That mis-ordering is what produced a nonsensical '-1d recovered' on
    peak-design-pro-tripod during the first dry-run, and left unfixed it would
    have let the safety invariant be evaded by a timezone rather than enforced.
    Naive values are assumed UTC — the assembler has always written aware UTC,
    so a naive one is legacy, and UTC is the only defensible reading of it."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def recover_mint(rel_path, stored, repo=ROOT):
    """Return (recovered_or_None, witness_label, refused_reason_or_None).

    recovered is None when nothing earlier than `stored` was found — the common
    healthy case for a card that has never been rebuilt."""
    a, _ = earliest_recorded_created_at(rel_path, repo=repo)
    b, _ = first_add_date(rel_path, repo=repo)
    candidates = [(_as_dt(v), v, name)
                  for v, name in ((a, 'recorded'), (b, 'first-add'))
                  if _as_dt(v) is not None]
    if not candidates:
        return None, 'no witness', None
    dt, value, label = min(candidates, key=lambda c: c[0])
    stored_dt = _as_dt(stored)
    if stored_dt is not None:
        if dt > stored_dt:
            return None, label, (
                f'candidate {value[:19]} is LATER than stored {stored[:19]} — '
                f'refusing to move a mint date forward')
        if dt == stored_dt:
            return None, label, None
    return value, label, None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _rel(path):
    """Path relative to the repo root, or None when it lies outside it.

    git witnesses only exist for files tracked in THIS repo, so a --cards-dir
    pointing elsewhere is not a crash (`relative_to` raises) but a knowable
    condition the caller reports."""
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='backfill_created_at.py',
        description='Recover destroyed freshness.created_at values from git.')
    ap.add_argument('--card', default=None,
                    help='Repair a single slug (default: every published card).')
    ap.add_argument('--commit', action='store_true',
                    help='Write the repairs. Default is a dry-run table.')
    ap.add_argument('--cards-dir', default=str(CARDS_DIR))
    args = ap.parse_args(argv)

    usable, why = repo_is_usable()
    if not usable:
        print(f'FATAL: {why}. The git history IS the evidence here; without it '
              f'this tool would write confident guesses.', file=sys.stderr)
        return EXIT_UNUSABLE_REPO

    cards_dir = Path(args.cards_dir)
    paths = ([cards_dir / f'{args.card}.json'] if args.card
             else sorted(cards_dir.glob('*.json')))

    repaired = unchanged = refused = 0
    print(f"{'slug':<27} {'stored':<11} {'recovered':<11} {'witness':<10} note")
    for path in paths:
        if not path.exists():
            print(f'{path.stem:<27} — card not found at {path}')
            refused += 1
            continue
        raw = path.read_text(encoding='utf-8')
        card = json.loads(raw)
        fresh = card.get('freshness') or {}
        stored = fresh.get('created_at') or ''
        rel = _rel(path)
        if rel is None:
            print(f'{path.stem:<27} — outside the repo ({path}); no git '
                  f'witnesses available')
            refused += 1
            continue

        recovered, witness, refusal = recover_mint(rel, stored)
        if refusal:
            print(f'{path.stem:<27} {stored[:10]:<11} {"—":<11} '
                  f'{witness:<10} REFUSED: {refusal}')
            refused += 1
            continue
        if not recovered:
            print(f'{path.stem:<27} {stored[:10]:<11} {"—":<11} '
                  f'{witness:<10} already earliest — unchanged')
            unchanged += 1
            continue

        days = ''
        s_dt, r_dt = _as_dt(stored), _as_dt(recovered)
        if s_dt and r_dt:
            days = f'({(s_dt - r_dt).days}d recovered)'
        print(f'{path.stem:<27} {stored[:10]:<11} {recovered[:10]:<11} '
              f'{witness:<10} {days}')
        repaired += 1

        if args.commit:
            # Byte-exact serialization (verified against all 11 published cards
            # 2026-07-27): indent=2, ensure_ascii=False, NO trailing newline.
            # Anything else buries a one-line repair in a whole-file diff.
            fresh['created_at'] = recovered
            card['freshness'] = fresh
            path.write_text(json.dumps(card, indent=2, ensure_ascii=False),
                            encoding='utf-8')

    verb = 'repaired' if args.commit else 'would repair'
    print(f'\n{verb}: {repaired}   unchanged: {unchanged}   refused: {refused}')
    if not args.commit and repaired:
        print('Dry-run — nothing written. Re-run with --commit to apply.')
    return EXIT_REFUSED if refused else EXIT_OK


if __name__ == '__main__':
    raise SystemExit(main())
