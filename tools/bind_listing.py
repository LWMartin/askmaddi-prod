#!/usr/bin/env python3
"""Bind a verified eBay listing onto spine entries that have none.

WHY (2026-11-18)
unbind_listing took wrong-product listings off 16 re-pointed cards, and nothing
binds a right one back: resolve_pass only resolves NEW proposals, no cron
revisits an existing entry, and a card rebuild reads the spine image without
re-resolving. Those cards show no eBay link, seller photo or used price.

The existing doors are unsafe here. backfill_skus writes through a plain upsert,
so the rebind firewall never runs. resolve_proposal runs it, but searches by the
spine's slug-shaped label ('Nikon 24mm f1.8'), and the firewall only blocks a
listing whose part number CONTRADICTS the authored one - a used F-mount AF-S
24mm f/1.8G listing that omits its part number passes, and would bind the wrong
lens back onto a Z card.

THE RULE: a listing is bound only on POSITIVE evidence. Its own getItem part
number must equal the entry's authored one (the slash colour suffix aside, the
rebind firewall's _core_mpn). A listing with no part number is never accepted,
however good its title. Every accepted listing also passes
rebind_firewall.assess (junk title, "for <product>" accessory, previously
unbound, authored-mpn contradiction), and the writer refuses any entry that is
bound by then and any listing on its unbound_listings.

SEARCH: brand + authored part number; when that yields nothing acceptable, once
more by the display name (sellers often leave a numeric part number out of the
title). A looser query widens what is LOOKED AT, never what is ACCEPTED - the
image second pass's rule. Bounded: at most --max-resolves getItem calls per
query, 0.5 s apart.

DRY RUN by default. Run on the box as askmaddi (the spine's writer):

  sudo -u askmaddi python3 tools/bind_listing.py --all-unbound
  sudo -u askmaddi python3 tools/bind_listing.py --slug nikon-24mm-f1-8 --commit

--all-unbound selects entries with no listing that carry unbind receipts: the
cards a human took a wrong listing off. An entry that never had a listing is a
different story (resolve_pass's), and is only touched by an explicit --slug.
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'gateway'))
sys.path.insert(0, str(ROOT / 'tools'))

import skus_registry  # noqa: E402
import rebind_firewall  # noqa: E402
from gtin_secondpass import _token_matches  # noqa: E402
from image_catalog_sweep import _load_gateway_env  # noqa: E402


def _norm(v):
    return ''.join(ch for ch in str(v or '').lower() if ch.isalnum())


def _legacy(item_id):
    """'v1|123|0' -> '123'; a bare id passes through."""
    parts = str(item_id or '').split('|')
    return parts[1] if len(parts) >= 2 else str(item_id or '')


def selected(skus, slugs=None, all_unbound=False):
    if slugs:
        return [s for s in slugs if s in skus], [s for s in slugs if s not in skus]
    out = []
    for s, e in sorted(skus.items()):
        if all_unbound and e.get('unbound_listings') and \
                not str(skus_registry.get_marketplace_id(e, 'ebay_legacy_item_id') or '').strip():
            out.append(s)
    return out, []


def judge(entry, resolved):
    """(accepted, reason) for one resolved candidate against a standing entry."""
    ident = entry.get('identity') or {}
    r = (resolved or {}).get('identity') or {}
    authored = ident.get('mpn', '')
    r_mpn = r.get('mpn', '')
    if not r_mpn or skus_registry._is_placeholder_mpn(r_mpn):
        return False, 'listing has no part number'
    if rebind_firewall._core_mpn(r_mpn) != rebind_firewall._core_mpn(authored):
        return False, f'part number {r_mpn!r} is not {authored!r}'
    brand, r_brand = _norm(ident.get('brand')), _norm(r.get('brand'))
    if brand and r_brand and brand != r_brand:
        return False, f'brand {r.get("brand")!r} is not {ident.get("brand")!r}'
    candidate = {'identity': dict(r),
                 'marketplace_ids': {'ebay_legacy_item_id': r.get('legacy_item_id', '')}}
    fw = rebind_firewall.assess(entry, candidate)
    if fw['verdict'] == 'reject':
        return False, 'firewall: ' + ','.join(fw['signals'])
    return True, 'part number matches'


def find(slug, entry, *, ebay, max_resolves=5, sleep_s=0.5):
    """Search and judge. Returns {'verdict', 'resolved', 'query', 'inspected'}.
    No writes."""
    ident = entry.get('identity') or {}
    brand, mpn = (ident.get('brand') or '').strip(), (ident.get('mpn') or '').strip()
    if not mpn or skus_registry._is_placeholder_mpn(mpn):
        return {'verdict': 'NO-AUTHORED-MPN', 'resolved': None, 'query': None, 'inspected': []}
    unbound = {str(r.get('item_id') or '').strip() for r in entry.get('unbound_listings') or []}
    name = (((entry.get('overrides') or {}).get('display_name')) or entry.get('label') or '').strip()
    queries = [f'{brand} {mpn}'.strip()]
    if name and name.lower() != queries[0].lower():
        queries.append(name)

    inspected, seen, calls, searched = [], set(), 0, 0
    for q in queries:
        try:
            cands = ebay.search_candidates(q, limit=20)
        except Exception as e:  # transient: report, never raise
            inspected.append({'query': q, 'error': str(e)})
            continue
        calls += 1
        searched += 1
        cands = [c for c in cands if c.get('item_id') not in seen]
        cands.sort(key=lambda c: 0 if _token_matches(c, mpn) else 1)
        spent = 0
        for c in cands:
            if spent >= max_resolves:
                break
            seen.add(c.get('item_id'))
            note = {'query': q, 'item_id': c.get('item_id'), 'title': c.get('title', '')}
            if _legacy(c.get('item_id')) in unbound:
                note['reason'] = 'previously unbound from this card'
                inspected.append(note)
                continue
            if sleep_s and calls:
                time.sleep(sleep_s)
            spent += 1
            calls += 1
            try:
                res = ebay.resolve(c['item_id'], customid=slug)
            except Exception as e:
                note['reason'] = f'resolve failed: {e}'
                inspected.append(note)
                continue
            ok, why = judge(entry, res)
            note['mpn'] = ((res.get('identity') or {}).get('mpn') or '')
            note['reason'] = why
            inspected.append(note)
            if ok:
                return {'verdict': 'FOUND', 'resolved': res, 'query': q, 'inspected': inspected}
    # Every search raised: say so, rather than claim the market has nothing.
    verdict = 'NOT-FOUND' if searched else 'SEARCH-FAILED'
    return {'verdict': verdict, 'resolved': None, 'query': queries[-1], 'inspected': inspected}


def run(*, slugs=None, all_unbound=False, commit=False, ebay=None, path=None,
        max_resolves=5, sleep_s=0.5, verbose=False, out=print):
    path = path or skus_registry.SKUS_PATH
    if ebay is None:
        _load_gateway_env()
        import ebay_api as ebay
    skus = skus_registry.load_registry(path).get('skus', {})
    targets, missing = selected(skus, slugs, all_unbound)
    for s in missing:
        out(f'  {s:28s} NOT ON SPINE')
    out(f"[bind] {len(targets)} entr{'y' if len(targets) == 1 else 'ies'} "
        f"({'COMMIT' if commit else 'dry-run'})")
    tally = {}
    for s in targets:
        entry = skus[s]
        if str(skus_registry.get_marketplace_id(entry, 'ebay_legacy_item_id') or '').strip():
            tally['skipped'] = tally.get('skipped', 0) + 1
            out(f'  {s:28s} ALREADY-BOUND')
            continue
        r = find(s, entry, ebay=ebay, max_resolves=max_resolves, sleep_s=sleep_s)
        line = f"  {s:28s} {r['verdict']}"
        if r['verdict'] == 'FOUND':
            ri = r['resolved']['identity']
            line += (f"  [{ri.get('legacy_item_id')}] mpn {ri.get('mpn')!r}"
                     f"\n      title: {ri.get('market_title', '')[:90]}")
            if commit:
                status = skus_registry.bind_listing(
                    s, r['resolved'],
                    {'method': 'part-number-match', 'query': r['query'], 'tool': 'bind_listing'},
                    path=path)
                line += f'\n      -> {status}'
                tally['bound'] = tally.get('bound', 0) + (status == 'bound')
        if verbose or r['verdict'] != 'FOUND':
            for n in r['inspected'][:12]:
                line += (f"\n      - {n.get('mpn', '')!r:14} {n.get('reason') or n.get('error', '')}"
                         f": {n.get('title', '')[:60]}")
        tally[r['verdict']] = tally.get(r['verdict'], 0) + 1
        out(line)
    out('[bind] ' + ', '.join(f'{k} {v}' for k, v in sorted(tally.items()))
        + ('' if commit else '  (dry-run, nothing written)'))
    return tally


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument('--slug', action='append', default=[])
    ap.add_argument('--all-unbound', action='store_true',
                    help='entries with no listing that carry unbind receipts')
    ap.add_argument('--commit', action='store_true')
    ap.add_argument('--max-resolves', type=int, default=5)
    ap.add_argument('--verbose', action='store_true')
    ap.add_argument('--path', default=None)
    a = ap.parse_args(argv)
    if not (a.slug or a.all_unbound):
        ap.error('pass --slug or --all-unbound')
    run(slugs=a.slug, all_unbound=a.all_unbound, commit=a.commit, path=a.path,
        max_resolves=a.max_resolves, verbose=a.verbose)
    return 0


if __name__ == '__main__':
    sys.exit(main())
