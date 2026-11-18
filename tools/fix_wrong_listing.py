#!/usr/bin/env python3
"""Take a wrong-product eBay listing off a spine entry (and, optionally, correct
the part number it stamped). The operator surface for skus_registry's
correct_mpn + unbind_listing doors.

WHEN

The card's name and reviews are one product; its bound eBay listing is another
(sony-a7s bound to an A7S II listing on 2026-09-15: wrong part number, wrong
title, wrong photo). Fix order is forced:

  1. correct_mpn  — so the image sweep searches the RIGHT part number, and the
                    rebind firewall has an authored number to defend;
  2. unbind       — removes the listing's title, photos, price, item id and
                    affiliate link, and keeps a receipt the firewall reads, so
                    the next resolve cannot bind the same listing back.

Then, separately: tools/image_catalog_sweep.py --slug <slug> finds the right
catalog photo by brand + corrected part number.

Both writes are compare-and-swap: the tool refuses unless the spine still holds
exactly the wrong values you name. Dry run is the default.

MUST RUN ON THE BOX AS askmaddi (the spine's only writer):

  sudo -u askmaddi python3 tools/fix_wrong_listing.py --slug sony-a7s \\
      --expect-item 358393938452 --expect-mpn ILCE7SM2/B --mpn ILCE7S/B \\
      --reason "listing is the A7S II"
  ... --apply

  sudo -u askmaddi python3 tools/fix_wrong_listing.py --plan tools/rename_plans/<plan>.json [--apply]

Exit 0 = done (or would be). Exit 1 = refused (spine does not hold what you
named). Exit 2 = slug missing.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'gateway'))
sys.path.insert(0, str(ROOT / 'tools'))

import skus_registry  # noqa: E402


def _show(entry):
    ident = entry.get('identity') or {}
    return {
        'mpn': ident.get('mpn'),
        'item_id': skus_registry.get_marketplace_id(entry, 'ebay_legacy_item_id'),
        'market_title': ident.get('market_title'),
        'image_catalog': ident.get('image_catalog'),
        'unbound_listings': [r.get('item_id') for r in entry.get('unbound_listings') or []],
    }


CARDS_DIR = ROOT / 'data' / 'cards'


def _card_pin(cards_dir, slug):
    """(card_path, card, pinned destination) — destination '' when no feed pin."""
    import price_writeback
    card_path = Path(cards_dir) / f'{slug}.json'
    if not card_path.is_file():
        return card_path, None, ''
    card = json.loads(card_path.read_text(encoding='utf-8'))
    p = card.get('pricing') or {}
    pin = p.get('current_new_url') or p.get('affiliate_url') or ''
    return card_path, card, price_writeback._dest(pin) if pin else ''


def run(slug, expect_item, reason, mpn=None, expect_mpn=None, apply=False,
        path=skus_registry.SKUS_PATH, out=sys.stdout, expect_new_pin=None,
        cards_dir=CARDS_DIR):
    """expect_new_pin: the Adorama destination (e.g. 'https://www.adorama.com/
    canon-ef-50-mm-f-1-2l-usm-lens/p/ca5012afu') the card is pinned to now. A pin
    matched by the OLD part number is void once the part number is corrected,
    so it is cleared with price_writeback.clear_new_pin — compare-and-swap like
    the spine writes: refused if the card is pinned anywhere else."""
    entry = skus_registry.load_registry(path).get('skus', {}).get(slug)
    if entry is None:
        print(f'{slug}: not on the spine', file=out)
        return 2
    print(f'{slug} now: {json.dumps(_show(entry), ensure_ascii=False)}', file=out)

    ident = entry.get('identity') or {}
    norm = lambda v: ''.join(ch for ch in str(v or '').lower() if ch.isalnum())
    bound = str(skus_registry.get_marketplace_id(entry, 'ebay_legacy_item_id') or '')
    problems = []
    if bound != str(expect_item):
        problems.append(f'bound listing is {bound!r}, not {expect_item!r}')
    if mpn is not None and norm(ident.get('mpn')) not in (norm(expect_mpn), norm(mpn)):
        problems.append(f'mpn is {ident.get("mpn")!r}, not {expect_mpn!r}')
    if expect_new_pin is not None:
        import price_writeback
        _, card, pinned = _card_pin(cards_dir, slug)
        want = price_writeback._dest(expect_new_pin)
        if card is None:
            problems.append(f'no card file for {slug} in {cards_dir}')
        elif pinned != want:
            problems.append(f'card is pinned to {pinned or "nothing"!r}, not {want!r}')
    if problems:
        for p in problems:
            print(f'REFUSED: {p}', file=out)
        return 1

    prov_mpn = {'method': 'wrong-listing-correction', 'reason': reason,
                'tool': 'fix_wrong_listing'}
    prov_unbind = {'method': 'unbind-wrong-product', 'reason': reason,
                   'tool': 'fix_wrong_listing'}
    if not apply:
        if mpn is not None:
            print(f'would correct mpn {ident.get("mpn")!r} -> {mpn!r}', file=out)
        print(f'would unbind listing {expect_item} ("{ident.get("market_title")}")', file=out)
        if expect_new_pin is not None:
            print(f'would clear the card\'s new-price pin {expect_new_pin}', file=out)
        print('DRY RUN — re-run with --apply', file=out)
        return 0

    if mpn is not None:
        r = skus_registry.correct_mpn(slug, mpn, prov_mpn, expect_mpn=expect_mpn, path=path)
        print(f'correct_mpn: {r}', file=out)
        if r not in ('corrected', 'no-op'):
            return 1
    r = skus_registry.unbind_listing(slug, expect_item, prov_unbind, path=path)
    print(f'unbind_listing: {r}', file=out)
    after = skus_registry.load_registry(path)['skus'][slug]
    print(f'{slug} after: {json.dumps(_show(after), ensure_ascii=False)}', file=out)
    if r != 'unbound':
        return 1
    if expect_new_pin is not None:
        import time
        import price_writeback
        card_path, card, _ = _card_pin(cards_dir, slug)
        receipt = price_writeback.clear_new_pin(
            card, f'identity re-pointed: {reason}',
            time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        price_writeback._write_card_atomic(card_path, card)
        print(f'cleared new-price pin: {receipt["url"]} (was ${receipt["price"]})', file=out)
    return 0


def run_plan(plan_path, apply=False, path=skus_registry.SKUS_PATH, out=sys.stdout,
             cards_dir=CARDS_DIR):
    """Every row of a plan file's `repoints` list, in order. Each row is checked
    and written on its own (compare-and-swap per row), so one refused row does
    not stop the rest; the exit code is the worst row's."""
    rows = json.loads(Path(plan_path).read_text(encoding='utf-8'))['repoints']
    worst = 0
    for row in rows:
        print(f"── {row['slug']}", file=out)
        rc = run(row['slug'], row['expect_item'], row['reason'], row.get('mpn'),
                 row.get('expect_mpn'), apply, path=path, out=out,
                 expect_new_pin=row.get('expect_new_pin'), cards_dir=cards_dir)
        worst = max(worst, rc)
    print(f"\n{len(rows)} row(s); worst exit {worst}", file=out)
    return worst


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--plan', help='a JSON file with a `repoints` list of rows '
                                   '{slug, expect_item, reason, mpn, expect_mpn}')
    ap.add_argument('--slug')
    ap.add_argument('--expect-item',
                    help='the wrong listing\'s eBay item id, as on the spine now')
    ap.add_argument('--reason')
    ap.add_argument('--mpn', help='the correct part number (optional)')
    ap.add_argument('--expect-mpn', help='the wrong part number on the spine now')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--path', default=str(skus_registry.SKUS_PATH))
    ap.add_argument('--cards-dir', default=str(CARDS_DIR))
    args = ap.parse_args(argv)
    if args.plan:
        return run_plan(args.plan, args.apply, path=args.path, cards_dir=args.cards_dir)
    if not (args.slug and args.expect_item and args.reason):
        ap.error('--slug, --expect-item and --reason are required without --plan')
    if (args.mpn is None) != (args.expect_mpn is None):
        ap.error('--mpn and --expect-mpn go together')
    return run(args.slug, args.expect_item, args.reason, args.mpn, args.expect_mpn,
               args.apply, path=args.path)


if __name__ == '__main__':
    sys.exit(main())
