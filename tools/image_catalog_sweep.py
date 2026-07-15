#!/usr/bin/env python3
"""
image_catalog_sweep.py — land eBay catalog stock images on spine entries that lack one.
================================================================================
The runner composing image_secondpass (evidence) + skus_registry.set_image_catalog
(the write). One mechanism, two jobs:

  ONE-SHOT (F2 replacement): run once over the veteran entries that predate
    images-on-spine — image_catalog lands WITHOUT forced re-resolve, and dead
    bound listings don't matter because the rescue searches the live market.
  STEADY-STATE (option 2): cron it after the 04:00 resolve pass — any fresh
    mint whose bound listing carried only a seller photo gets its stock shot
    hunted within a day. The /admin badge covers the interim.

DRY-RUN BY DEFAULT (backfill_skus discipline): prints per-slug verdicts and a
summary; nothing is written until --commit. Human overrides and existing
catalog images are never touched (enforced in BOTH the selector and the
writer — belt and suspenders).

Politeness: bounded by --max-resolves getItems per SKU and --limit SKUs per
run, with the same inter-call sleep as the GTIN sweep. A full-spine nightly
run at current scale is a handful of Browse calls, well under the resolve
pass's own footprint.

Usage (on the box, as askmaddi — needs gateway/.env eBay creds):
  python3 tools/image_catalog_sweep.py                 # dry-run, all needing rescue
  python3 tools/image_catalog_sweep.py --slug sony-a7c # one SKU
  python3 tools/image_catalog_sweep.py --commit        # write rescued images
  python3 tools/image_catalog_sweep.py --commit --limit 10
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / 'gateway'))


def _load_gateway_env():
    """Source gateway/.env before importing ebay_api (reads env at import).

    Mirrors backfill_skus._load_gateway_env: no external dep, only sets keys
    not already present, silent no-op if the file is absent.
    """
    env_path = ROOT / 'gateway' / '.env'
    if not env_path.exists():
        return
    import os
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


def run(*, slug=None, commit=False, limit=None, max_resolves=3,
        sleep_s=0.5, ebay=None, registry_path=None, out=print, verbose=False):
    """Sweep the spine; returns the summary dict (also printed).

    Injectable ebay + registry_path keep this unit-testable offline, same
    discipline as the factory's injected runner.
    """
    import image_secondpass as isp
    import skus_registry

    path = registry_path or skus_registry.SKUS_PATH
    registry = skus_registry.load_registry(path)
    skus = registry.get('skus', {}) or {}

    if slug:
        if slug not in skus:
            out(f"[sweep] no spine entry {slug!r}")
            return {'error': 'missing-slug', 'slug': slug}
        targets = {slug: skus[slug]}
    else:
        targets = {s: e for s, e in skus.items() if isp.needs_rescue(e)}

    if limit:
        targets = dict(list(targets.items())[:int(limit)])

    out(f"[sweep] {len(targets)} entr{'y' if len(targets) == 1 else 'ies'} "
        f"need{'s' if len(targets) == 1 else ''} a catalog image "
        f"({'COMMIT' if commit else 'dry-run'})")

    summary = {'rescued': 0, 'written': 0, 'no_catalog': 0,
               'no_candidates': 0, 'skipped': 0, 'errors': 0, 'per_slug': {}}

    for s, entry in targets.items():
        res = isp.rescue_catalog_image(s, entry, ebay=ebay,
                                       max_resolves=max_resolves,
                                       sleep_s=sleep_s)
        verdict = res['verdict']
        line = f"  {s:34s} {verdict}"
        if verdict == isp.RESCUED:
            summary['rescued'] += 1
            line += f"  {res['image_catalog']}"
            if commit:
                status = skus_registry.set_image_catalog(
                    s, res['image_catalog'], res['image_provenance'],
                    path=path)
                line += f"  -> {status}"
                if status == 'written':
                    summary['written'] += 1
        elif verdict == isp.NO_CATALOG_FOUND:
            summary['no_catalog'] += 1
            if verbose:
                for n in res.get('inspected', []):
                    tag = ('ERR ' + n['error'] if n.get('error')
                           else ('no-imageUrls' if not n.get('had_catalog')
                                 else n.get('rejected', '?')))
                    line += f"\n      - {n.get('epid','')} {tag}: {n.get('title','')[:70]}"
                line += f"\n      query: {res.get('query')}" 
        elif verdict == isp.NO_CANDIDATES:
            summary['no_candidates'] += 1
        elif verdict in (isp.HAS_CATALOG, isp.SKIPPED_OVERRIDE, isp.NO_KEYS):
            summary['skipped'] += 1
        else:
            summary['errors'] += 1
        summary['per_slug'][s] = verdict
        out(line)

    out(f"[sweep] rescued {summary['rescued']}"
        + (f", written {summary['written']}" if commit else " (dry-run, nothing written)")
        + f", no-catalog {summary['no_catalog']}, no-candidates {summary['no_candidates']}"
        + f", skipped {summary['skipped']}, errors {summary['errors']}")
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Rescue eBay catalog stock images for spine entries lacking one.")
    ap.add_argument('--slug', help='Sweep a single SKU.')
    ap.add_argument('--commit', action='store_true',
                    help='Write rescued images to the spine (default: dry-run).')
    ap.add_argument('--limit', type=int, help='Max SKUs to sweep this run.')
    ap.add_argument('--max-resolves', type=int, default=3,
                    help='Max getItem calls per SKU (default 3).')
    ap.add_argument('--verbose', action='store_true',
                    help='Print per-candidate evidence on NO-CATALOG-FOUND.')
    ap.add_argument('--json', action='store_true',
                    help='Also print the summary as JSON (cron-friendly).')
    args = ap.parse_args(argv)

    _load_gateway_env()
    summary = run(slug=args.slug, commit=args.commit, limit=args.limit,
                  max_resolves=args.max_resolves, verbose=args.verbose)
    if args.json:
        print(json.dumps(summary))
    return 0 if not summary.get('error') else 1


if __name__ == '__main__':
    sys.exit(main())
