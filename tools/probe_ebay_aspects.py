#!/usr/bin/env python3
"""
probe_ebay_aspects.py — would the eBay payload we ALREADY fetch fill spec slots?

`ebay_api.resolve()` calls getItem with fieldgroups=PRODUCT. Live probe evidence
recorded in ebay_api.py (2026-07-15) shows the product container returning
[additionalProductIdentities, aspectGroups, brand, gtins, image, mpns, title].
`aspectGroups` is eBay's CATALOG spec structure — grouped name/value pairs, the
shape of a spec sheet. It arrives inside `_raw` on every resolve, skus_registry
states plainly that `_raw` is not persisted, and nothing in either repo reads
aspectGroups: the only mentions are comments.

So we may be paying for a spec source and discarding it. This probe answers
whether it is worth an adapter BEFORE anyone writes one — the same
measure-then-build order that produced the 21/44 naming-register number.

WHAT IT MEASURES

  1. COVERAGE — how many live SKUs return aspectGroups at all. The product
     container is documented in ebay_api.py as present on only SOME listings,
     roughly those created through eBay's catalog flow, so this is genuinely
     unknown rather than assumed.
  2. YIELD — how many FACTS slots those aspects would fill, scored through the
     REAL fact_pipeline.slotfill.fill_slots, not a bespoke counter. That makes
     the number directly comparable to the recorded benchmarks: 21/44 for a
     Sony body from the help guide, 3/5 for a Sigma lens.
  3. THE ALIAS DEBT — the advisory rung-1b substring delta. Expect the register
     number to be LOW here and do not read that as "eBay has no data": the
     aliases were authored only from observed Sony/Sigma help-guide names, so a
     new surface starts at almost zero by construction. This is precisely the
     case rung 1b was kept as a MEASURING INSTRUMENT for (ruling 2026-07-28) —
     where it exceeds the register, the difference names the aliases we owe.
     Its hits are NOT trustworthy on their own: measured offline against eBay's
     vocabulary it maps `battery_model` onto "Lithium-Ion" (a chemistry, not a
     model) and `weight_g` onto "22.05 Oz" (needs conversion). A to-do list for
     a human, never a filler.

eBay group names map onto SpecCandidate.section, which is what the harvester's
mandatory section scope wants — so the comparison is like-for-like rather than
flattering to eBay.

WHAT IT DOES NOT DO

Writes nothing. No skus.json change, no card change, no adapter. Pure
inspection, same posture as probe_gtin_in_payload.py.

Slot scoring needs the AUTHORED dictionaries in phantom-ops. If they are not
reachable the probe still reports COVERAGE and says loudly that scoring did not
run. That is a deliberate departure from the refuse-don't-skip rule used by
verify_rebuilt_card: there, a partial check could be mistaken for a pass. Here
the two halves answer different questions under different labels, and a
coverage number cannot be misread as a yield number.

RUN ON THE BOX as askmaddi — that account owns the eBay credentials, the same
reason probe_gtin_in_payload.py names it:

  sudo -u askmaddi bash -lc 'cd /home/askmaddi/askmaddi-prod && \
    python3 tools/probe_ebay_aspects.py --aggregator-root <phantom-ops>/claude/workspace/aggregator-build'

Exit 0 = probe ran. Exit 2 = could not reach the eBay gateway modules at all.
"""
import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'gateway'))
sys.path.insert(0, 'gateway')

# ebay_api reads EBAY_APP_ID / EBAY_CERT_ID at MODULE level, so the .env must
# load BEFORE the import or every resolve() fails "not configured" without
# touching eBay. Same bootstrap probe_gtin_in_payload.py and resolve_pass.py do.
try:
    import env_bootstrap
    env_bootstrap.load_dotenv()
except ImportError:
    pass

try:
    import ebay_api
    import skus_registry
except ImportError as exc:
    print(f'FATAL: cannot import gateway modules ({exc}). '
          f'Run from the askmaddi-prod root.', file=sys.stderr)
    sys.exit(2)


def load_scoring(aggregator_root):
    """The scoring callables, or None — printing WHY, distinguishing causes.

    The first live run printed "no aggregator root reachable" when the root
    was reachable and the real cause was a missing bs4. Diagnosing the wrong
    thing sends the reader to fix the wrong thing.
    """
    if not aggregator_root:
        print('  [!] no --aggregator-root passed; scoring needs the authored '
              'dictionaries.', file=sys.stderr)
        return None
    root = Path(aggregator_root)
    if not (root / 'fact_pipeline' / 'slotfill.py').exists():
        print(f'  [!] {root} is not an aggregator-build directory (no '
              f'fact_pipeline/slotfill.py) — path or permissions.',
              file=sys.stderr)
        return None
    sys.path.insert(0, str(root))
    try:
        from fact_pipeline.slotfill import fill_slots
        from fact_pipeline.harvest import SpecCandidate
        from fact_pipeline.benchmark_deterministic import substring_fill
        from dictionaries import load_facts
    except ImportError as exc:
        missing = str(exc).split("'")[1] if "'" in str(exc) else str(exc)
        print(f'  [!] the aggregator root IS reachable; a DEPENDENCY is '
              f'missing: {missing}', file=sys.stderr)
        print(f'  [!] fact_pipeline/__init__.py re-exports harvest, which '
              f'imports bs4+lxml, so even slotfill pulls them in.',
              file=sys.stderr)
        print(f'  [!] remedy: pip install --user beautifulsoup4 lxml   '
              f'(as the account running this probe)', file=sys.stderr)
        return None
    return fill_slots, SpecCandidate, load_facts, substring_fill


def candidates_from(raw, SpecCandidate, source_id):
    """Lift SpecCandidates out of an eBay payload.

    Two sources, kept apart because they are not equally trustworthy:
      aspectGroups     — eBay CATALOG data, grouped. The group name becomes the
                         section, which is what the harvester's mandatory scope
                         needs and is why this is comparable to a spec sheet.
      localizedAspects — item-level, flat, seller-influenced. Sectionless, so it
                         is reported separately rather than blended in.
    """
    product = raw.get('product') or {}
    catalog, item_level = [], []

    for group in (product.get('aspectGroups') or []):
        section = (group.get('localizedGroupName') or '',)
        for asp in (group.get('aspects') or []):
            name = asp.get('localizedName') or ''
            values = asp.get('localizedValues') or []
            if not name or not values:
                continue
            catalog.append(SpecCandidate(
                key=name, value='; '.join(str(v) for v in values),
                section=tuple(s for s in section if s),
                host='api.ebay.com', source_id=source_id))

    for asp in (raw.get('localizedAspects') or []):
        name, value = asp.get('name') or '', asp.get('value') or ''
        if name and value:
            item_level.append(SpecCandidate(
                key=name, value=str(value), section=(),
                host='api.ebay.com', source_id=source_id))

    return catalog, item_level


def ebay_item_id(entry):
    """The eBay listing id, from wherever this registry generation keeps it.

    CORRECTED 2026-07-29 after the first live run reported NO-LEGACY-ID for
    all 14 SKUs. The current schema holds it at
    `marketplace_ids.ebay_legacy_item_id` (present on 14/14); the
    `identity.legacy_item_id` this probe originally read — copied from
    probe_gtin_in_payload.py — exists on none of them. That sibling probe has
    the same defect and is silently dead against the live registry.

    Both are read, newest first, because a probe that reports "no id" when the
    id is right there is worse than one that fails loudly: it looks like a
    finding about eBay coverage.
    """
    mkt = entry.get('marketplace_ids') or {}
    return (mkt.get('ebay_legacy_item_id')
            or (entry.get('identity') or {}).get('legacy_item_id')
            or '')


def category_of(entry):
    facet = entry.get('facet')
    return str(entry.get('category')
               or (facet.get('category') if isinstance(facet, dict) else facet)
               or '')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument('--aggregator-root', default=None,
                    help='phantom-ops aggregator-build (enables slot scoring)')
    ap.add_argument('--slug', default=None, help='probe one SKU instead of all')
    ap.add_argument('--dump', default=None,
                    help='write the full per-SKU findings as JSON here')
    args = ap.parse_args(argv)

    scoring = load_scoring(args.aggregator_root)
    if scoring is None:
        print('!! SLOT SCORING DID NOT RUN — see the reason above.')
        print('!! Coverage below is real; YIELD is simply unmeasured.\n')
    fill_slots = SpecCandidate = load_facts = substring_fill = None
    if scoring:
        fill_slots, SpecCandidate, load_facts, substring_fill = scoring

    registry = skus_registry.load_registry()
    skus = registry.get('skus') or {}
    if args.slug:
        skus = {args.slug: skus[args.slug]} if args.slug in skus else {}
        if not skus:
            print(f'no such slug: {args.slug}', file=sys.stderr)
            return 2

    verdicts, findings = Counter(), {}
    print(f'{"SKU":30s} {"catalog":>8s} {"item":>6s} {"filled":>12s}  verdict')
    print('-' * 78)

    for slug, entry in sorted(skus.items()):
        legacy = ebay_item_id(entry)
        if not legacy:
            verdicts['NO-LEGACY-ID'] += 1
            print(f'{slug:30s} {"-":>8s} {"-":>6s} {"-":>12s}  NO-LEGACY-ID')
            continue
        try:
            res = ebay_api.resolve(f'v1|{legacy}|0')
        except Exception as exc:
            verdicts['RESOLVE-FAILED'] += 1
            print(f'{slug:30s} {"-":>8s} {"-":>6s} {"-":>12s}  '
                  f'RESOLVE-FAILED ({type(exc).__name__})')
            continue

        raw = res.get('_raw') or {}
        if SpecCandidate is None:
            product = raw.get('product') or {}
            n_cat = sum(len(g.get('aspects') or [])
                        for g in (product.get('aspectGroups') or []))
            n_item = len(raw.get('localizedAspects') or [])
            cat_list = item_list = []
        else:
            cat_list, item_list = candidates_from(raw, SpecCandidate, slug)
            n_cat, n_item = len(cat_list), len(item_list)

        verdict = 'ASPECTS' if n_cat else ('ITEM-ONLY' if n_item else 'NONE')
        verdicts[verdict] += 1

        filled = '-'
        rec = {'catalog_aspects': n_cat, 'item_aspects': n_item,
               'verdict': verdict}
        if fill_slots and (cat_list or item_list):
            category = category_of(entry)
            try:
                total = sum(len(v) for v in load_facts(category).values())
                # Catalog first: sectioned, and the tier worth an adapter.
                fills, _ = fill_slots(cat_list, category)
                both, _ = fill_slots(cat_list + item_list, category)
                # Advisory only — the alias debt, not a yield. See module doc.
                facts = load_facts(category)
                slot_map = {s['slot']: axis
                            for axis, ss in facts.items() for s in ss}
                loose = substring_fill(cat_list + item_list, slot_map)
                owed = sorted(set(loose) - set(both))
                filled = f'{len(fills)}/{total} (+{len(owed)})'
                rec.update(slots_total=total, filled_catalog=len(fills),
                           filled_with_item=len(both), category=category,
                           slots=sorted(fills), alias_debt=owed,
                           alias_debt_values={k: loose[k] for k in owed})
            except Exception as exc:
                filled = f'score-failed'
                rec['score_error'] = f'{type(exc).__name__}: {exc}'

        findings[slug] = rec
        print(f'{slug:30s} {n_cat:>8d} {n_item:>6d} {filled:>12s}  {verdict}')

    print('-' * 78)
    print('verdicts:', dict(verdicts))
    scored = [r for r in findings.values() if 'filled_catalog' in r]
    if scored:
        print(f'\nYIELD (catalog aspects only, scored through the real '
              f'fill_slots):')
        for slug, r in sorted(findings.items()):
            if 'filled_catalog' not in r:
                continue
            print(f'  {slug:30s} {r["category"]:8s} '
                  f'{r["filled_catalog"]}/{r["slots_total"]}'
                  f'   (+item-level: {r["filled_with_item"]})')
        print('\nBenchmarks to beat, from the manufacturer surfaces:')
        print('  body  21/44  (Sony help guide, naming register 2026-07-28)')
        print('  lens   3/5   (Sigma, same)')
        print('\nThese are COMPLEMENTARY, not competing — eBay is precedence 4,')
        print('manufacturer 2. The question an adapter answers is what eBay')
        print('fills where no manufacturer surface exists (7 of 14 SKUs today).')

        debt = {}
        for r in scored:
            for slot, val in (r.get('alias_debt_values') or {}).items():
                debt.setdefault(slot, set()).add(val)
        if debt:
            print('\nALIAS DEBT — what rung 1b reaches that the register does')
            print('not. Each line is an alias a human should VET and author,')
            print('not a fact to trust (1b maps battery_model onto a chemistry).')
            for slot in sorted(debt):
                vals = ', '.join(sorted(debt[slot])[:3])
                print(f'  {slot:26s} <- {vals}')
        else:
            print('\nNo rung-1b delta: eBay names either already alias or do')
            print('not resemble any slot. The first is good news, the second')
            print('means an adapter would need model rungs, not aliases.')

    if args.dump:
        Path(args.dump).write_text(json.dumps(findings, indent=2))
        print(f'\nfull findings -> {args.dump}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
