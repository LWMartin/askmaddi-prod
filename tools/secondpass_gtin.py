#!/usr/bin/env python3
"""
secondpass_gtin.py — registry-level GTIN recovery sweep (substrate step 5b).
========================================================================
Sweeps skus.json for entries where identity.gtin is null/absent and recovers
a GTIN with strict provenance precedence:

  1. OWN LISTING FIRST — re-resolve the SKU's own legacy_item_id and read the
     L1 extraction off its own payload (true first-pass provenance; no gate
     needed — no cross-listing corroboration is happening). This heals the
     provenance inversion: pre-substrate-5 entries have null gtin even when
     their own payload carries one (7/14 measured 2026-06-30).
  2. SECOND PASS on miss — barren payload, dead listing (used goods sell),
     or no legacy_item_id: eBay brand+mpn search -> catalog-associated
     candidates -> 4-clause admission gate (gtin_secondpass.recover_gtin).

This is THE production entry point for L2 — a registry-level pass,
deliberately NOT a hook inside any mint path (see gtin_secondpass module
docstring: recursion, tap-path latency, review_queue's frozen-identity
contract). All three mint paths (tap/capture, factory, backfill) converge on
the same spine, so one sweep heals every path, including future express mints.

WRITE DISCIPLINE
  - Dry-run by default; --commit to write. Same convention as backfill_skus.
  - Writes ONLY via skus_registry.set_gtin (upgrade-only: a non-null L1 GTIN
    is never overwritten; upsert can't carry a gtin-only change).
  - Persists on ADMIT (gtin + provenance) and CONFLICT-DROP (null gtin +
    conflict-flagged provenance, for /admin visibility). Every other drop
    writes NOTHING — those SKUs stay re-attemptable as new listings appear.

Usage:
  python3 tools/secondpass_gtin.py                     # dry-run, all null-gtin SKUs
  python3 tools/secondpass_gtin.py --card sony-a7iv    # one SKU
  python3 tools/secondpass_gtin.py --commit            # write ADMIT + CONFLICT
Sandbox has no creds; this is a box tool (runs in /opt/askmaddi-prod).
"""
import argparse
import json
import sys

# --- env FIRST, before any transitive ebay_api import (L1's latent-bug fix) ---
sys.path.insert(0, "gateway")
import env_bootstrap  # noqa: E402
env_bootstrap.load_dotenv()

import ebay_api  # noqa: E402
import gtin_secondpass as sp  # noqa: E402
import skus_registry  # noqa: E402

# Verdicts that persist a write under --commit.
WRITE_VERDICTS = (sp.OWN_LISTING_L1, sp.ADMIT, sp.CONFLICT_DROP)


def null_gtin_tail(skus):
    """Entries whose identity.gtin is falsy — covers BOTH pre-L1 entries
    (no gtin key at all) and L1-null mints. .get() treats them identically."""
    return {slug: e for slug, e in skus.items()
            if not (e.get('identity', {}) or {}).get('gtin')}


def recover(entry, *, ebay, max_resolves=5):
    """Per-SKU recovery: OWN LISTING FIRST, second-pass search only on miss.

    Ordering rationale (provenance inversion, first live sweep 2026-07-01):
    pre-substrate-5 entries carry null gtin even when their own payload has
    one; the own listing's L1 GTIN is strictly stronger provenance than a
    cross-listing recovery and needs no gate. Fall-through cases: barren own
    payload, dead listing (used goods sell), no legacy_item_id.
    """
    ident = entry.get('identity', {}) or {}

    own = sp.recover_own_listing(ident.get('legacy_item_id'), ebay=ebay)
    if own['verdict'] == sp.OWN_LISTING_L1:
        own['query'] = None
        return own

    second = sp.recover_gtin(
        ident.get('brand'), ident.get('mpn'),
        model=entry.get('model') or ident.get('market_title'),
        ebay=ebay, max_resolves=max_resolves)
    # Keep the own-listing outcome in the report so a fall-through is auditable.
    second['own_listing'] = own['verdict']
    return second


def run(skus_path='data/skus.json', card=None, commit=False,
        max_resolves=5, limit=None, ebay=ebay_api, out=print):
    reg = json.load(open(skus_path))
    skus = reg.get('skus', reg)

    tail = null_gtin_tail(skus)
    if card:
        if card not in skus:
            out(f'ERROR: slug {card!r} not in registry')
            return 2
        if card not in tail:
            out(f'{card}: identity.gtin already set — upgrade-only, nothing to do')
            return 0
        tail = {card: tail[card]}
    if limit:
        tail = dict(list(tail.items())[:limit])

    mode = 'COMMIT' if commit else 'DRY-RUN'
    out('=' * 64)
    out(f'  GTIN SECOND-PASS SWEEP — {mode}')
    out(f'  null-gtin tail: {len(tail)} SKU(s) of {len(skus)} in registry')
    out('=' * 64)

    summary = {}
    for i, (slug, entry) in enumerate(tail.items(), 1):
        res = recover(entry, ebay=ebay, max_resolves=max_resolves)

        verdict = res['verdict']
        summary.setdefault(verdict.split(':')[0], []).append(slug)

        out(f"\n=== [{i}/{len(tail)}] {slug} ===")
        if verdict == sp.OWN_LISTING_L1:
            prov = res['gtin_provenance'] or {}
            out(f"    own listing: GTIN {res['gtin']}  src={prov.get('chosen_source')}"
                f"  conflict={prov.get('conflict')}")
        else:
            out(f"    own listing: {res.get('own_listing', '(skipped)')}"
                f"  ->  second pass")
            out(f"    query:   {res['query']!r}")
            rec = ((res.get('gtin_provenance') or {}).get('recovery') or {})
            for c in rec.get('candidates', []):
                if c.get('error'):
                    out(f"      - {c['item_id']}  ERROR {c['error']}")
                else:
                    out(f"      - epid={c.get('epid')}  gtin={c.get('gtin')}  "
                        f"src={c.get('chosen_source')}  tok={c.get('token_match')}  "
                        f"\"{c.get('title', '')}\"")
        out(f"    VERDICT: {verdict}"
            + (f"  ->  GTIN {res['gtin']}" if res['gtin'] else ""))

        if verdict in WRITE_VERDICTS:
            if commit:
                status = skus_registry.set_gtin(
                    slug, res['gtin'], res['gtin_provenance'], path=skus_path)
                out(f"    WRITE:   {status}")
            else:
                out(f"    WRITE:   (dry-run — would persist "
                    f"{'gtin+provenance' if res['gtin'] else 'conflict receipt'})")

    out('\n' + '-' * 64)
    out('SWEEP SUMMARY:')
    for v, slugs in sorted(summary.items()):
        out(f'  {v:22s} {len(slugs):2d}  {", ".join(slugs)}')
    out('-' * 64)
    n = len(tail)
    l1 = len(summary.get(sp.OWN_LISTING_L1, []))
    admitted = len(summary.get(sp.ADMIT, []))
    conflicts = len(summary.get(sp.CONFLICT_DROP, []))
    out(f'Recovered: {l1} own-listing L1 + {admitted} second-pass of {n}   '
        f'(+{conflicts} conflict -> /admin)')
    out('Residual tail falls to Gemma+title fallback (designed behavior).')
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--card', help='sweep a single slug (default: all null-gtin)')
    ap.add_argument('--skus', default='data/skus.json')
    ap.add_argument('--commit', action='store_true',
                    help='persist ADMIT + CONFLICT verdicts (default: dry-run)')
    ap.add_argument('--max-resolves', type=int, default=5,
                    help='per-SKU candidate resolve cap (API politeness)')
    ap.add_argument('--limit', type=int, help='cap SKUs swept this run')
    args = ap.parse_args()
    sys.exit(run(skus_path=args.skus, card=args.card, commit=args.commit,
                 max_resolves=args.max_resolves, limit=args.limit))


if __name__ == '__main__':
    main()
