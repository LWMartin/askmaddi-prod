#!/usr/bin/env python3
"""
migrate_substrate.py — hoist settled skus.json entries to the substrate shape.

The one-shot migration the substrate spec's migration note names (spec:
maddi-product-substrate, "Schema" section + Amendment A). Per entry:

  Axis A (identity):
    identity.gtin            -> entry.gtin           (canonical anchor; null ok)
    identity.epid            -> marketplace_ids.ebay_epid
    identity.legacy_item_id  -> marketplace_ids.ebay_legacy_item_id
    affiliate.amazon_asin    -> marketplace_ids.amazon_asin   (MIRRORED — the
                                affiliate block itself is unchanged, it serves
                                the affiliate flow; marketplace_ids is the
                                identity-shadow home)
    identity.gtin_provenance -> STAYS in identity. The anchor is canonical,
                                the receipt is evidence; the architecture
                                demotes evidence deliberately (adjudication
                                events ride along untouched — append-only).

  Axis B (classification):
    category                 -> facet                (verbatim vocab, renamed)
    identity.ebay_category_id-> marketplace_categories.ebay_category_id
    unspsc                   -> null                 (Gemma mapper backfills)

  Contract:
    needs_review             -> gtin is null OR unspsc is null (the substrate's
                                per-entry abstain flag; distinct from the
                                slug-level SlugResolution.needs_review and from
                                minted_needs_review, both untouched). unspsc is
                                null spine-wide until the mapper ships, so this
                                truthfully flips every entry — Axis B is
                                genuinely unmapped, and no live consumer reads
                                the per-entry field yet (verified 2026-07-02).

  Invariants:
    - FROZEN SLUGS untouched — keys of registry['skus'] never change.
    - IDEMPOTENT by shape detection: an entry with 'marketplace_ids' is
      already migrated and is skipped verbatim (mints from the new
      build_entry land in this shape natively). Re-runs are no-ops.
    - APPEND-ONLY at the registry level: a `substrate` receipt list on the
      registry records each migrating run (version, at, counts) — runs
      append, never overwrite, same Crucible discipline as adjudications.
    - --dry-run is the DEFAULT; --commit writes (atomic via
      skus_registry._atomic_write). Dry-run prints the per-entry hoist plan.

Run on the box as the askmaddi user over /opt/askmaddi-prod/data/skus.json
after the code pull; sandbox/dev runs point --skus at a copy.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'gateway'))
import skus_registry  # noqa: E402

SUBSTRATE_VERSION = 2


def already_migrated(entry):
    """Shape detection — the idempotence key. New-shape mints and previously
    migrated entries both carry marketplace_ids; nothing else ever has."""
    return isinstance(entry.get('marketplace_ids'), dict)


def migrate_entry(entry):
    """Hoist ONE old-shape entry in place. Returns a change-report dict.

    Pure structural transform — no network, no judgment calls, no value
    remapping. Everything it moves is a verbatim relocation; everything it
    adds is a declared substrate default (unspsc null, recomputed
    needs_review).
    """
    identity = entry.setdefault('identity', {})
    affiliate = entry.get('affiliate') or {}

    gtin = identity.pop('gtin', None)
    epid = identity.pop('epid', '')
    legacy = identity.pop('legacy_item_id', '')
    ebay_cat = identity.pop('ebay_category_id', '')
    facet = entry.pop('category', '')

    entry['gtin'] = gtin
    entry['marketplace_ids'] = {
        'ebay_epid': epid,
        'ebay_legacy_item_id': legacy,
        'amazon_asin': affiliate.get('amazon_asin'),
    }
    entry['unspsc'] = None
    entry['facet'] = facet
    entry['marketplace_categories'] = {'ebay_category_id': ebay_cat}
    entry['needs_review'] = entry['gtin'] is None or entry['unspsc'] is None

    return {
        'gtin': gtin,
        'ebay_epid': epid,
        'ebay_legacy_item_id': legacy,
        'ebay_category_id': ebay_cat,
        'facet': facet,
        'needs_review': entry['needs_review'],
        'has_provenance': isinstance(identity.get('gtin_provenance'), dict),
        'has_adjudications': bool(
            (identity.get('gtin_provenance') or {}).get('adjudications')
            if isinstance(identity.get('gtin_provenance'), dict) else False),
    }


def run(skus_path='data/skus.json', commit=False, out=print):
    registry = skus_registry.load_registry(skus_path)
    skus = registry.get('skus', {})

    migrated, skipped = {}, []
    for slug in sorted(skus):
        entry = skus[slug]
        if already_migrated(entry):
            skipped.append(slug)
            continue
        migrated[slug] = migrate_entry(entry)

    mode = 'COMMIT' if commit else 'DRY-RUN'
    out(f'substrate migration v{SUBSTRATE_VERSION} [{mode}] — '
        f'{len(migrated)} to migrate, {len(skipped)} already substrate-shape')
    for slug in skipped:
        out(f'  = {slug}  (skipped, already migrated)')
    for slug, rep in migrated.items():
        anchor = rep['gtin'] or 'null'
        prov = ('receipt+adjudications' if rep['has_adjudications']
                else 'receipt' if rep['has_provenance'] else 'no receipt')
        out(f'  > {slug}')
        out(f'      gtin={anchor}  ({prov}, stays in identity)')
        out(f"      marketplace_ids: epid={rep['ebay_epid'] or '-'} "
            f"legacy={rep['ebay_legacy_item_id'] or '-'}")
        out(f"      facet={rep['facet'] or '-'}  "
            f"ebay_category_id={rep['ebay_category_id'] or '-'}  "
            f"unspsc=null  needs_review={rep['needs_review']}")

    if not migrated:
        out('nothing to do.')
        return 0

    if not commit:
        out('dry-run only — re-run with --commit to write.')
        return 0

    import time
    registry.setdefault('substrate', []).append({
        'version': SUBSTRATE_VERSION,
        'migrated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'migrated': len(migrated),
        'skipped': len(skipped),
    })
    registry['as_of'] = time.strftime('%Y-%m-%d', time.gmtime())
    skus_registry._atomic_write(registry, skus_path)
    out(f'written: {skus_path}  '
        f'(substrate receipt appended, {len(migrated)} entries hoisted)')
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        description='Hoist skus.json to the substrate shape (Axis A/B). '
                    'Dry-run by default.')
    p.add_argument('--skus', default='data/skus.json',
                   help='registry path (default: data/skus.json)')
    p.add_argument('--commit', action='store_true',
                   help='actually write (default is dry-run)')
    args = p.parse_args(argv)
    return run(skus_path=args.skus, commit=args.commit)


if __name__ == '__main__':
    sys.exit(main())
