#!/usr/bin/env python3
"""Author per-SKU spec-surface fragments into the protected overrides layer.

The R10 brand table (phantom-ops `aggregator-build/spec_surfaces.py`) owns the
host and the URL template; a SKU owes only the fragments that fill it. Before
this ran, no live SKU carried any, so `resolve` reported 8 SKUs as
`surface_fragments_unset` and the manufacturer tier — precedence 2, the whole
target of the harvester — contributed nothing to production.

WHY ITS OWN CARRIED KEY

`data/skus.json` is wholesale replaced on the upsert 'updated' path whenever
identity changes, and only layers named in `_merge_enrichment`'s carry list
survive. R10 first satisfied that by reusing `overrides`, which is on the
list — but `overrides` is ALSO the top layer of the card-identity merge
(maddi-images-on-spine D1/D3), so a fragment there reaches the rendered card.
The requirement was rotation survival, not `overrides`. `spec_surface` is its
own carried key with its own doctrine rule, matching the other four concerns.

WHICH GUIDE A SKU CITES (ruled by Lee, 2026-07-29)

A camera can have several manufacturer spec surfaces keyed by firmware
version: the ILCE-7SM3 publishes one guide for Ver.3-or-later and another for
earlier software, and the ILCE-1 does the same at Ver.2. The ruling is to
author the best estimate of the CURRENT specs as found, and on any future
re-derivation to take the most current locatable at that time.

That rule only works if a later pass can see what this pass picked, so each
entry carries `observed` (the page actually read) and `curated_at` beside the
fragments. Both are inert: `resolve_url` formats only the fragment names the
brand table declares, so undeclared keys pass through untouched.

WHAT IT REFUSES

The fragments must rebuild `observed` exactly. Authoring these means reading a
code off a page and typing it into a table, and a transcription slip would
produce a URL that resolves, fetches politely, parses cleanly and describes the
wrong product — success-shaped. So every entry is round-tripped through the
real brand table before anything is written, and a mismatch refuses the whole
run rather than writing the good ones.

A SKU that needs fragments but is not in the roster is NAMED, not skipped.
sony-a7r is deliberately absent: it carries an empty mpn and a model string of
just "A7R", which names no generation, and it was already held for
adjudication on 2026-07-26. Identity verification runs upstream of the merge,
so guessing here would launder wrong-product facts into an honest-spread range.

    python3 tools/backfill_spec_fragments.py
    python3 tools/backfill_spec_fragments.py --apply

Dry run is the default.

Exit 0 = done (or nothing to do).
Exit 1 = a SKU needs fragments that this roster does not cover.
Exit 2 = could not check (brand table unavailable, or a round-trip mismatch).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway import skus_registry  # noqa: E402

DEFAULT_AGGREGATOR_ROOT = (
    Path.home() / 'phantom-ops' / 'claude' / 'workspace' / 'aggregator-build')

CURATED_AT = '2026-07-29'

# slug -> (fragments, observed page). `observed` is the page actually read,
# not a page derived from the fragments — that is what makes the round-trip
# check meaningful rather than circular.
ROSTER = {
    'sony-a7iv': (
        {'tree': '2110', 'doc_id': 'TP1000660153'},
        'https://helpguide.sony.net/ilc/2110/v1/en/contents/'
        'TP1000660153.html'),
    'sony-a7s-iii': (
        {'tree': '2410', 'doc_id': '231h_specifications_ilc2410'},
        'https://helpguide.sony.net/ilc/2410/v1/en/contents/'
        '231h_specifications_ilc2410.html'),
    'sony-a7-v': (
        {'tree': '2540', 'doc_id': '251h_specifications_ilc2540'},
        'https://helpguide.sony.net/ilc/2540/v1/en/contents/'
        '251h_specifications_ilc2540.html'),
    'sony-a1': (
        {'tree': '2420', 'doc_id': '231h_specifications_ilc2420'},
        'https://helpguide.sony.net/ilc/2420/v1/en/contents/'
        '231h_specifications_ilc2420.html'),
    'sony-a7c': (
        {'tree': '2020', 'doc_id': 'TP1000156734'},
        'https://helpguide.sony.net/ilc/2020/v1/en/contents/'
        'TP1000156734.html'),
    'sigma-35mm-f1-2-dg-dn-art': (
        {'product_code': 'a019_35_12'},
        'https://www.sigma-global.com/en/lenses/a019_35_12/'),
    'sigma-35-art-dg-dn-ii': (
        {'product_code': 'a026_35_14'},
        'https://www.sigma-global.com/en/lenses/a026_35_14/'),
    # Peak Design 'support' surface (Shopify-Hydrogen {handle}); handles derived
    # from the card model via ingest.product_feed.derive_handle and validated
    # live 2026-08-05 (HTTP 200 + remix specs harvested: travel=6, pro=14).
    'peak-design-travel-tripod': (
        {'handle': 'travel-tripod'},
        'https://www.peakdesign.com/products/travel-tripod'),
    'peak-design-pro-tripod': (
        {'handle': 'pro-tripod'},
        'https://www.peakdesign.com/products/pro-tripod'),
    # GoPro 'action_cam' surface (Next.js {page_path}); the first video-vertical
    # SKU. page_path is slug + GoPro's non-derivable catalogue SKU (measured
    # 2026-08-21: HERO11=CHDHX-111, HERO12=CHDHX-121 resolve, a guessed
    # HERO13=CHDHX-131 500s). Carries its own curated_at because the surface was
    # authored 2026-08-21, after this roster's original 2026-07-29 batch.
    'gopro-hero10': (
        {'page_path': 'hero10-black/CHDHX-101-master'},
        'https://gopro.com/en/us/shop/cameras/hero10-black/'
        'CHDHX-101-master.html',
        '2026-08-21'),
}

# A SKU whose brand HAS a surface that does not cover it. R10's gap table is
# keyed by (brand, category) and cannot say this: sony/body works fine, five
# SKUs resolve through it. Recorded here so the SKU reports a measured absence
# instead of sitting at `fragments_unset`, which reads as curation debt and
# sends the next person looking for a page that does not exist.
SURFACE_GAPS = {
    'gopro-hero6': (
        'GoPro delists discontinued models from its storefront: the HERO6 '
        'page (CHDHX-601) 500s (measured 2026-08-21), so the gopro/action_cam '
        'Next.js surface — which serves current models fine (HERO10 banked) — '
        'cannot reach it. Not curation debt on the surface; the model is gone '
        'from it. Legacy-body specs would need a different source.'),
    'sony-a7r': (
        'the ILCE-7RM2 help guide (tree 1520) has no Specifications page — '
        'verified 2026-07-29 against its complete topic list, not inferred '
        'from section names. Sony did not publish one in guides of that era: '
        'every guide measured that HAS one (1950, 2020, 2110, 2410, 2420, '
        '2540) is 2019 or later. Not curation debt; there is nothing to '
        'curate on this surface.'),
}

# Named so an absence is a recorded decision, not an oversight. Pinned by a
# test so a future edit has to be deliberate about admitting it.
HELD_FOR_ADJUDICATION = {}


class BrandTableUnavailable(Exception):
    """The authored brand table could not be loaded.

    Refusing beats reporting. A tool that silently skipped verification would
    write unvalidated fragments and print the same success line either way.
    """


def load_brand_table(aggregator_root):
    """Import the authored R10 table, or raise BrandTableUnavailable."""
    root = Path(aggregator_root)
    if not (root / 'spec_surfaces.py').exists():
        raise BrandTableUnavailable(
            f'no spec_surfaces.py under {root} — pass --aggregator-root')
    sys.path.insert(0, str(root))
    try:
        import spec_surfaces
    except ImportError as exc:                       # pragma: no cover
        raise BrandTableUnavailable(str(exc)) from exc
    return spec_surfaces


def check_vocabulary_drift(aggregator_root):
    """Compare the two CARD_IDENTITY_FIELDS declarations, or return a reason.

    The set is co-declared: askmaddi-prod gates writes on it, phantom-ops
    reads the merge it protects. Neither repo depends on the other, so no CI
    run sees both — but THIS tool loads both by definition, which makes it the
    honest place to catch drift. Returns None when they agree.
    """
    ac = Path(aggregator_root) / 'assemble_card.py'
    if not ac.exists():
        return f'no assemble_card.py under {aggregator_root}'
    sys.path.insert(0, str(Path(aggregator_root)))
    try:
        from assemble_card import CARD_IDENTITY_FIELDS as READ_SIDE
    except ImportError as exc:
        return f'could not import the read-side vocabulary: {exc}'
    write_side = skus_registry.CARD_IDENTITY_FIELDS
    if set(READ_SIDE) != set(write_side):
        return ('CARD_IDENTITY_FIELDS disagree between repos — '
                f'phantom-ops only: {sorted(set(READ_SIDE) - set(write_side))}, '
                f'askmaddi-prod only: {sorted(set(write_side) - set(READ_SIDE))}')
    return None


def brand_and_category(entry):
    """Read (brand, category) out of the live spine's vocabulary."""
    facet = entry.get('facet')
    category = (entry.get('category')
                or (facet.get('category') if isinstance(facet, dict) else facet)
                or '')
    return str(entry.get('vendor') or entry.get('brand') or ''), str(category)


def _authoring_conflicts():
    """Slugs declared BOTH a gap and a set of fragments.

    The gap asserts there is nothing to fetch; the fragments say where to
    fetch it. resolve() resolves this deterministically in the gap's favour,
    but a writer that allowed both would make the contradiction invisible.
    """
    return sorted(set(ROSTER) & set(SURFACE_GAPS))


def plan(registry, table):
    """Return (writes, uncovered, mismatches) without touching anything.

    writes:     [(slug, override_value)] ready to set
    uncovered:  [(slug, reason)] needs fragments, roster has none
    mismatches: [(slug, expected, built)] round-trip failed
    """
    writes, uncovered, mismatches = [], [], []
    for slug, entry in (registry.get('skus') or {}).items():
        brand, category = brand_and_category(entry)
        if table.surface_for(brand, category) is None:
            continue                      # declared gap or unknown pair: R12's
        if slug in SURFACE_GAPS:
            writes.append((slug, {'gap': SURFACE_GAPS[slug],
                                  'curated_at': CURATED_AT}))
            continue
        if slug not in ROSTER:
            uncovered.append(
                (slug, HELD_FOR_ADJUDICATION.get(slug, 'not in roster')))
            continue
        roster_entry = ROSTER[slug]
        fragments, observed = roster_entry[0], roster_entry[1]
        # A roster entry may carry its own curated_at (3rd element) when it was
        # authored outside this roster's original batch date; otherwise it takes
        # the batch constant.
        curated_at = roster_entry[2] if len(roster_entry) > 2 else CURATED_AT
        built = table.resolve_url(brand, category, fragments)
        if built != observed:
            mismatches.append((slug, observed, built))
            continue
        writes.append((slug, dict(fragments,
                                  observed=observed,
                                  curated_at=curated_at)))
    return writes, uncovered, mismatches


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--apply', action='store_true',
                    help='write the overrides (default is a dry run)')
    ap.add_argument('--aggregator-root', default=str(DEFAULT_AGGREGATOR_ROOT),
                    help='phantom-ops aggregator-build directory')
    ap.add_argument('--skus-path', default=None,
                    help='override the registry path (tests)')
    args = ap.parse_args(argv)

    path = Path(args.skus_path) if args.skus_path else skus_registry.SKUS_PATH

    try:
        table = load_brand_table(args.aggregator_root)
    except BrandTableUnavailable as exc:
        print(f'REFUSED: {exc}', file=sys.stderr)
        print('The brand table owns the URL template; without it these '
              'fragments cannot be verified, and writing them unverified is '
              'the failure this tool exists to prevent.', file=sys.stderr)
        return 2

    conflicts = _authoring_conflicts()
    if conflicts:
        print(f'REFUSED: {conflicts} are declared as BOTH a surface gap and a '
              f'set of fragments — one says there is nothing to fetch, the '
              f'other says where. Pick one.', file=sys.stderr)
        return 2

    drift = check_vocabulary_drift(args.aggregator_root)
    if drift is not None:
        print(f'REFUSED: {drift}', file=sys.stderr)
        return 2

    registry = skus_registry.load_registry(path)
    writes, uncovered, mismatches = plan(registry, table)

    if mismatches:
        print('REFUSED — authored fragments do not rebuild the page that was '
              'read:', file=sys.stderr)
        for slug, expected, built in mismatches:
            print(f'  {slug}\n    read:  {expected}\n    built: {built}',
                  file=sys.stderr)
        return 2

    for slug, value in writes:
        detail = ', '.join(f'{k}={v}' for k, v in value.items()
                           if k not in ('observed', 'curated_at'))
        if args.apply:
            status = skus_registry.set_spec_surface(slug, value, path=path)
        else:
            status = 'would-write'
        print(f'{status:12s} {slug:28s} {detail}')

    for slug, reason in uncovered:
        print(f'{"UNCOVERED":12s} {slug:28s} {reason}')

    print(f'\n{len(writes)} covered, {len(uncovered)} uncovered'
          + ('' if args.apply else '  (dry run — pass --apply to write)'))
    return 1 if uncovered else 0


if __name__ == '__main__':
    sys.exit(main())
