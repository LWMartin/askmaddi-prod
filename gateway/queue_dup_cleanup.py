#!/usr/bin/env python3
"""One-shot maintenance: surface EXISTING cross-slug product-identity dups to /admin.

The mint-time dedup gate (resolve_sku, spec maddi-multisource-identity-matcher)
stops NEW duplicate cards from resurrecting, but it is forward-looking. Dups that
minted BEFORE the gate already sit on the spine — two slugs sharing a real
product id (MPN / GTIN / eBay epid). This script finds those clusters and
enqueues ONE review item per cluster (reason 'duplicate_identity_contradiction')
so nothing dangles: the reviewer either rejects the junior slug as `duplicate`
(a true dup, e.g. Skydio 2 x2) or corrects the wrong slug's identity (a mis-stamp,
e.g. ILCE7RM5B on both sony-a7r and sony-a7-v).

Placeholder MPNs ('Dose not apply' / N/A / ...) are NEVER a join key — the same
skus_registry._is_placeholder_mpn guard the gate uses (so Avata 2 and Mavic 4 Pro,
which both carry a placeholder live, are not falsely clustered).

Idempotent: review_queue.enqueue dedups by queue_id, so re-running never forks a
record. DRY-RUN by default (prints the plan); pass --commit to write the queue.

Run ON THE BOX AS askmaddi (never root — root-owned service-tree files break the
gateway; see phantom-ops memory `phantom-ops-onbox-root-ownership`). Paths default
to the module-relative live stores (data/skus.json, data/review_queue.json), so
running from /opt targets the live /opt stores by construction.
"""
import argparse
import collections
import sys

import review_queue
import skus_registry
import resolve_sku


def _id_keys(entry):
    """The non-placeholder product-identity keys an entry joins on: (kind, value).
    Mirrors skus_registry.find_by_product_identity's join set exactly."""
    keys = []
    mpn = skus_registry._identity_mpn(entry)          # '' if absent OR placeholder
    if mpn:
        keys.append(('mpn', skus_registry._norm_join_mpn(mpn)))
    gtin = ''.join(ch for ch in str(skus_registry.get_gtin(entry) or '') if ch.isdigit())
    if gtin:
        keys.append(('gtin', gtin))
    epid = str(skus_registry.get_marketplace_id(entry, 'ebay_epid') or '').strip()
    if epid:
        keys.append(('epid', epid))
    return keys


_KIND_RANK = {'mpn': 0, 'gtin': 1, 'epid': 2}   # strongest id first


def find_clusters(registry):
    """Return [(kind, value, [slugs])] — ONE cluster per unique slug-set.

    A slug-pair can share more than one id (the Autel pair shares mpn AND gtin AND
    epid). Grouping by (kind, value) alone would enqueue the same pair three times,
    so clusters are collapsed by their slug-set; the representative id is the
    STRONGEST kind shared (mpn > gtin > epid), which is also the join the reviewer
    should reason about first."""
    by_id = collections.defaultdict(list)
    for slug, entry in (registry.get('skus') or {}).items():
        for kind, value in _id_keys(entry):
            by_id[(kind, value)].append(slug)

    # slug-set -> list of (kind, value) ids it shares
    by_slugset = collections.defaultdict(list)
    for (kind, value), slugs in by_id.items():
        if len(slugs) > 1:
            by_slugset[frozenset(slugs)].append((kind, value))

    clusters = []
    for slugset, ids in by_slugset.items():
        kind, value = min(ids, key=lambda kv: (_KIND_RANK.get(kv[0], 9), kv[1]))
        clusters.append((kind, value, sorted(slugset)))
    return sorted(clusters, key=lambda c: c[2])


def _classify(registry, slugs):
    """'clean_dup' if the first two members look like the same product (per the
    gate's own _model_family_agrees arbiter), else 'contradiction' (mis-stamp /
    successor). Reported only — the human always makes the final call at /admin."""
    a = registry['skus'][slugs[0]]
    b = registry['skus'][slugs[1]]
    agrees = resolve_sku._model_family_agrees(
        a.get('vendor'), a.get('model'), b.get('vendor'), b.get('model'))
    return 'clean_dup' if agrees else 'contradiction'


def queue_clusters(registry, clusters, *, skus_path, queue_path, commit):
    """Enqueue one review item per cluster. The JUNIOR slug (2nd, alphabetically)
    is the enqueue subject; the SENIOR (1st) rides in collision_with so /admin's
    duplicate-identity badge names the sibling. Frozen identity comes from the
    junior's spine entry (no eBay round-trip)."""
    queued = []
    for kind, value, slugs in clusters:
        senior, junior = slugs[0], slugs[1]
        j = registry['skus'][junior]
        verdict = _classify(registry, slugs)
        identity = dict(j.get('identity') or {})
        # Freeze the junior's demoted marketplace shadows into the identity block
        # so the record is self-contained (enqueue reads identity, not the entry).
        identity.setdefault('epid', skus_registry.get_marketplace_id(j, 'ebay_epid') or '')
        resolved = {'identity': identity,
                    'affiliate_url': (j.get('affiliate') or {}).get('ebay_epn_url', '')}
        res = resolve_sku._FactoryResolution(
            slug=junior,
            input_text=f'shared {kind} {value} with {senior} ({len(slugs)} slugs: {", ".join(slugs)})')
        res.collision = senior     # -> record.collision_with, the badge detail
        rec = {'kind': kind, 'value': value, 'slugs': slugs, 'verdict': verdict,
               'junior': junior, 'senior': senior}
        if commit:
            out = review_queue.enqueue(
                res, resolved, j.get('vendor', ''), j.get('model', ''),
                skus_registry.get_facet(j) or '',
                contamination_key=j.get('contamination_key'),
                reason_override='duplicate_identity_contradiction',
                path=queue_path)
            rec['queue_id'] = out['queue_id']
            rec['status'] = out['status']
        queued.append(rec)
    return queued


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--commit', action='store_true',
                    help='Write the review queue (default: dry-run, print plan only).')
    ap.add_argument('--skus-path', default=str(skus_registry.SKUS_PATH))
    ap.add_argument('--queue-path', default=str(review_queue.REVIEW_QUEUE_PATH))
    args = ap.parse_args(argv)

    registry = skus_registry.load_registry(args.skus_path)
    clusters = find_clusters(registry)
    mode = 'COMMIT' if args.commit else 'DRY-RUN'
    print(f"[queue_dup_cleanup] {mode} — {len(clusters)} cross-slug id cluster(s) "
          f"in {args.skus_path}")
    if not clusters:
        print("  (nothing to queue — no cross-slug shared identities)")
        return 0

    queued = queue_clusters(registry, clusters, skus_path=args.skus_path,
                            queue_path=args.queue_path, commit=args.commit)
    for rec in queued:
        flag = 'CLEAN DUP -> reject junior as `duplicate`' if rec['verdict'] == 'clean_dup' \
            else 'CONTRADICTION -> adjudicate (mis-stamp/successor)'
        qid = f"  [{rec.get('queue_id', 'dry')}]" if args.commit else '  [dry]'
        print(f"{qid} {rec['kind']}={rec['value']!r}: {', '.join(rec['slugs'])}")
        print(f"        {flag}")
    if not args.commit:
        print("\n  Re-run with --commit to enqueue these into /admin.")
    else:
        print(f"\n[queue_dup_cleanup] enqueued {len(queued)} item(s) -> {args.queue_path}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
