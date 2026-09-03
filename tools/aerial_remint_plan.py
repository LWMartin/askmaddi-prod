#!/usr/bin/env python3
"""aerial_remint_plan.py — READ-ONLY review artifact for the aerial delist+re-mint.

WHY THIS EXISTS (2026-09-03, aerial pre-fix residue)
The spine-model canonicalizer landed at mint (resolve_sku.lookup_or_mint, commit
dbafdd5) and kills the doubled-brand + listing-cruft slug class GOING FORWARD.
But the drone SKUs minted BEFORE that fix are frozen in the spine under dirty
slugs (`dji-dji-air-3s-dual-1-cmos-45min-flight`), resolve to no spec source, and
sit `corpus_thin`/`failed` — they never become cards. The ruling is delist +
re-mint (slug is an immutable identity anchor; never hand-rename).

This tool STAGES that operation for human review. It does NOT try to predict the
exact clean re-mint slug — that can't be derived reliably from a stale spine model
(the authoritative clean form is whatever the current eBay spool holds). Instead
it leans on two signals it CAN compute honestly:

  1. CONTAMINATION  a spine slug is a delist target if it is doubled-brand
     (`dji-dji-…`) OR its model fails spine_canonicalize.is_clean_model().
  2. RE-MINT FUEL   whether the live eBay spool currently carries a proposal for
     the SAME product (core-identity match: brand + identity tokens, condition/
     listing words stripped). Fuel present → re-mint restores it; fuel absent →
     delisting drops coverage until the tap re-lists it (flagged loudly).

Per row it also reports the queue state, whether a built card file exists, and
whether the slug sits in the resolve-attempts ledger (a possible re-block snag).
The `--emit-slugs` file feeds delist_card.py directly.

READ-ONLY: loads/prints only — writes nothing but the optional --emit-slugs file.
Safe as root.

  python3 tools/aerial_remint_plan.py
  python3 tools/aerial_remint_plan.py --emit-slugs data/aerial-remint-slugs.txt
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'gateway'))
import skus_registry        # noqa: E402
import spine_canonicalize   # noqa: E402
import work_queue           # noqa: E402

# Aerial vertical detection: brand OR keyword. Over-inclusive on purpose — a false
# positive that is actually clean lands in CLEAN and is filtered from the targets.
_AERIAL_BRANDS = {
    'dji', 'autel', 'hoverair', 'zerozero', 'skydio', 'parrot',
    'potensic', 'ryze', 'holystone', 'wefone', 'droneer',
}
_AERIAL_KW = re.compile(
    r'\b(drone|mavic|avata|osmo|inspire|phantom|ronin|quadcopter|fpv|goggles'
    r'|neo|mini-\d|air-\d|evo)\b', re.I)

# Doubled leading brand: 'dji-dji-…', 'autel-autel-…', 'hoverair-hoverair-…'.
_DOUBLED_BRAND = re.compile(r'^([a-z0-9]+)-\1(?:-|$)')

# Listing/condition noise to drop when reducing a title to its identity core, so
# 'DJI Mavic 2 Pro Only Flies Great' and a spool 'DJI Mavic 2 Pro' match. This is
# for MATCHING ONLY — never for minting a slug.
_STOP = {
    'usa', 'in', 'stock', 'shipping', 'ship', 'only', 'read', 'pristine',
    'condition', 'never', 'opened', 'w', 'rc', 'factory', 'premium', 'display',
    'unit', 'flies', 'great', 'new', 'sealed', 'box', 'bundle', 'the', 'and',
    'with', 'for', 'like', 'mint', 'used', 'excellent', 'good', 'refurbished',
    'oem', 'genuine', 'authentic', 'fast', 'free', 'lot', 'set', 'kit', 'combo',
}
_WORD = re.compile(r'[a-z0-9]+')


def _is_aerial(slug, vendor, model):
    v = (vendor or '').strip().lower().replace(' ', '')
    if v in _AERIAL_BRANDS:
        return True
    return bool(_AERIAL_KW.search(f"{slug} {vendor} {model}".lower()))


def _core(vendor, model):
    """Identity-token signature for cross-store matching: brand + significant
    tokens, condition/listing noise and pure-duplicate brand dropped."""
    vtok = set(_WORD.findall((vendor or '').lower()))
    # keep multi-char tokens AND lone digits ('mavic 2' vs 'mavic 3' must not
    # collapse) — drop only single stray letters and listing/condition noise.
    toks = [t for t in _WORD.findall((model or '').lower())
            if t not in _STOP and (len(t) > 1 or t.isdigit())]
    # collapse a leading duplicate brand token ('dji dji mavic' -> 'dji mavic')
    core = []
    for t in toks:
        if core and core[-1] == t:
            continue
        core.append(t)
    return frozenset(core) | vtok


def _match(target_core, spool_cores):
    """True if some spool proposal shares this product's identity core. Uses
    containment (the smaller significant set inside the larger), min 2 tokens."""
    for sc in spool_cores:
        common = target_core & sc
        smaller = min(len(target_core), len(sc))
        if smaller >= 2 and len(common) >= smaller:
            return True
    return False


def _load_spool_cores(spool_path):
    p = Path(spool_path)
    if not p.exists():
        return None
    try:
        rows = json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return None
    rows = rows if isinstance(rows, list) else rows.get('proposals', [])
    return [_core(r.get('vendor', ''), r.get('model', ''))
            for r in rows if isinstance(r, dict)]


def _load_ledger(ledger_path):
    p = Path(ledger_path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return None


def build_plan(*, skus_path, queue_path, spool_path, ledger_path):
    reg = skus_registry.load_registry(skus_path)
    skus = reg.get('skus') or {}
    queue = work_queue.load_queue(queue_path).get('queue', {})
    spool_cores = _load_spool_cores(spool_path)
    ledger = _load_ledger(ledger_path)

    rows = []
    for slug, entry in skus.items():
        vendor = entry.get('vendor', '')
        model = entry.get('model', '')
        if not _is_aerial(slug, vendor, model):
            continue
        doubled = bool(_DOUBLED_BRAND.match(slug))
        unclean = not spine_canonicalize.is_clean_model(vendor, model)
        contaminated = doubled or unclean
        will_remint = (_match(_core(vendor, model), spool_cores)
                       if spool_cores is not None else None)
        qrec = queue.get(slug) or {}
        rows.append({
            'slug': slug,
            'contaminated': contaminated,
            'reason': 'doubled-brand' if doubled else ('cruft' if unclean else '-'),
            'state': qrec.get('state', '-'),
            'has_card': (Path(skus_path).parent / 'cards' / f'{slug}.json').exists(),
            'will_remint': will_remint,
            'in_ledger': (slug in ledger) if ledger is not None else None,
        })
    rows.sort(key=lambda r: (not r['contaminated'], r['will_remint'] is not True,
                             r['state'], r['slug']))
    return rows


def _fmt(v):
    return {True: 'Y', False: '-', None: '?'}.get(v, v)


def main(argv=None):
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument('--skus-path', default=str(root / 'data' / 'skus.json'))
    ap.add_argument('--queue-path', default=str(root / 'data' / 'work_queue.json'))
    ap.add_argument('--spool-path', default='/home/askmaddi/pipeline/ebay-proposals.json',
                    help='eBay proposals spool (re-mint fuel check). Best-effort.')
    ap.add_argument('--ledger-path', default=str(root / 'data' / 'resolve-attempts.json'))
    ap.add_argument('--emit-slugs', default=None,
                    help='Write the delist target slugs, one per line.')
    args = ap.parse_args(argv)

    rows = build_plan(skus_path=args.skus_path, queue_path=args.queue_path,
                      spool_path=args.spool_path, ledger_path=args.ledger_path)
    targets = [r for r in rows if r['contaminated']]
    refuel = [r for r in targets if r['will_remint'] is True]
    drop = [r for r in targets if r['will_remint'] is False]
    unknown = [r for r in targets if r['will_remint'] is None]

    print("════ AERIAL RE-MINT PLAN ════")
    print(f"  aerial spine entries: {len(rows)}  ·  contaminated (delist targets): {len(targets)}")
    if targets and targets[0]['will_remint'] is not None:
        print(f"  of those → {len(refuel)} have spool fuel (delist→re-mint)  ·  "
              f"{len(drop)} have NO spool fuel (delist→drop, review)")
    else:
        print("  (spool not readable here → re-mint fuel shown as '?'; run on the box for Y/-)")

    print(f"\n  {'reason':13} {'state':12} card remint ledger  slug")
    for r in targets:
        print(f"  {r['reason']:13} {r['state']:12} {_fmt(r['has_card']):>4} "
              f"{_fmt(r['will_remint']):>6} {_fmt(r['in_ledger']):>6}  {r['slug']}")

    if drop:
        print(f"\n  ⚠ {len(drop)} contaminated with NO clean twin in the eBay spool — "
              f"delisting drops coverage until the tap re-lists them. Review before --apply:")
        for r in drop:
            print(f"      {r['slug']}")
    ledgered = [r for r in targets if r['in_ledger']]
    if ledgered:
        print(f"\n  ⚠ {len(ledgered)} sit in the resolve-attempts ledger — clear those keys "
              f"alongside the delist so a cooling/decontaminated mark can't re-block re-mint.")
    carded = [r for r in targets if r['has_card']]
    if carded:
        print(f"\n  ℹ {len(carded)} have a built card file → run delist_card --files-only "
              f"(commit+land) for these, not just --spine-only:")
        for r in carded:
            print(f"      {r['slug']}")

    if args.emit_slugs:
        Path(args.emit_slugs).write_text(
            '\n'.join(r['slug'] for r in targets) + '\n', encoding='utf-8')
        print(f"\n  → {len(targets)} delist target slug(s) written to {args.emit_slugs}")
    print("════ END ════")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
