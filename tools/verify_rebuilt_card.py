#!/usr/bin/env python3
"""Check a rebuilt card before it is approved at /admin/publish.

The seven bodies requeued on 2026-07-27 come back through the drip one at a
time, each one waiting on a human at the publish gate. This is what that human
runs first, because the defect being repaired is invisible on the rendered
page: a camera body extracted with the lens dictionary looks like a finished
card. It has headings, percentages, sources and prose. It is simply answering
questions nobody asked about a camera body, and silently missing the sensor,
EVF, burst and battery discussion its reviewers actually produced.

An eye cannot check that. A dictionary lookup can.

WHAT IS CHECKED

  1. Vocabulary — every axis on the card belongs to the card's own category,
     and at least one axis is EXCLUSIVE to that category. The second half
     matters: universal axes (price, weight, build, handling) appear on
     everything, so a mis-extracted body still carries some of them. Only an
     exclusive axis proves the right dictionary was loaded.
  2. Labels — every axis has a display_name, and it matches the authored
     LABELS facet. This is what keeps a raw or dotted axis id out of card
     prose, the meta description and the schema.org description, which all
     read from the same strings.
  3. Mint date — created_at must equal the published card's. A rebuild that
     resets it destroys provenance recovered from git on 2026-07-27, and the
     factory path is the one resolve_prior_created_at has to survive.
  4. staleness_days — must be absent. Its presence means the card came from
     the legacy writer and the fix did not reach this build.
  5. Grounding — sentiment shares appear as pos/neu/neg or not at all, and no
     exhaustiveness language reaches the prose.

WHY THE AXES ARE DERIVED AND NOT LISTED HERE

The dictionary is authored in phantom-ops and is explicitly going to grow —
video equipment and flash are named next, drones get their own pocket
dictionary. A copy of the axis names in this repo would be correct today and
wrong at the first extension, and it would be wrong SILENTLY, in a verifier,
which is the worst place for it. That is the same drift that let the lens
synthesizer keep two ungrounded sentences for three days after the classifier
was fixed. So the sets are computed from the dictionary at run time, and a
new category becomes checkable without touching this file.

WHY A MISSING DICTIONARY IS FATAL

If phantom-ops is not reachable, this exits non-zero and says so. It does not
print "label check skipped" and exit clean. On 2026-07-27 requeue_rebuild's
first version reported "0 requeueable, 7 blocked" — output indistinguishable
from a genuinely empty spool — and that nearly bought a full re-fetch of seven
cards. A verifier whose silence can mean either "passed" or "did not run" is
worse than no verifier.

    python3 tools/verify_rebuilt_card.py /var/lib/askmaddi-cards/sony-a7s-iii/card.json
    python3 tools/verify_rebuilt_card.py <new> --published data/cards/sony-a7s-iii.json
    PHANTOM_OPS=/home/phantomops/phantom-ops python3 tools/verify_rebuilt_card.py <new>

Exit 0 = safe to approve. Exit 1 = do not approve. Exit 2 = could not check.
"""
import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_DICT_ROOT = os.environ.get('PHANTOM_OPS', '/home/phantomops/phantom-ops')

# Phrases that assert we read everything. We read what we gathered.
EXHAUSTIVE = ('all reviews', 'every review', 'exhaustive', 'complete list')

_AGG = ('claude', 'workspace', 'aggregator-build')


class DictionaryUnavailable(RuntimeError):
    """phantom-ops is not reachable, so nothing here can be checked."""


def load_dictionary(dict_root):
    """Return (load_axes, load_labels, available_categories) from phantom-ops.

    Seam: tests replace this wholesale rather than staging a fake checkout.
    """
    agg = Path(dict_root).joinpath(*_AGG)
    if not agg.is_dir():
        raise DictionaryUnavailable(
            f'no aggregator-build under {dict_root} — pass --dict-root or set '
            f'PHANTOM_OPS to a phantom-ops checkout')
    sys.path.insert(0, str(agg))
    try:
        from dictionaries import (available_categories, load_axes,  # noqa: E402
                                  load_labels)
    except ImportError as exc:  # pragma: no cover - import-time environment
        raise DictionaryUnavailable(f'dictionaries package unimportable: {exc}')
    return load_axes, load_labels, available_categories


def universal_axes(load_axes, categories):
    """Axes shared by every category — the ones that prove nothing.

    Computed as the intersection rather than read from _universal directly,
    because what makes an axis useless as EVIDENCE here is that it appears
    everywhere, which is a property of the merged result.
    """
    sets = [set(load_axes(c)) for c in categories]
    return set.intersection(*sets) if sets else set()


def card_category(card):
    """identity.category is the authority.

    Top-level `category` is deprecated and null on most cards; identity is
    correct on all of them. Reading the deprecated one would make this tool
    unable to check the very cards it was written for.
    """
    return (card.get('identity') or {}).get('category')


def card_axes(card):
    """Axis ids on the card, from every block that keys by one."""
    found = set()
    for key in ('lead_axes', 'detail_axes'):
        for item in card.get(key) or []:
            if isinstance(item, dict) and item.get('axis_id'):
                found.add(item['axis_id'])
    found |= set((card.get('confidence') or {}).get('per_axis') or {})
    for item in (card.get('synthesis') or {}).get('aspect_breakdown') or []:
        if isinstance(item, dict) and item.get('aspect'):
            found.add(item['aspect'])
    return found


def card_labels(card):
    """axis_id -> display_name as this card actually renders it."""
    out = {}
    for key in ('lead_axes', 'detail_axes'):
        for item in card.get(key) or []:
            if isinstance(item, dict) and item.get('axis_id'):
                out[item['axis_id']] = item.get('display_name')
    return out


def share_triples(paragraph):
    """Runs of three percentages, as the grounded wording emits them."""
    import re
    return re.findall(r'(\d+)%[^.]{0,60}?(\d+)%[^.]{0,60}?(\d+)%', paragraph)


def has_percentage(paragraph):
    import re
    return bool(re.search(r'\d+%', paragraph))


def check(card, published=None, dict_root=DEFAULT_DICT_ROOT, loader=None):
    """Return (fails, warns, notes). Raises DictionaryUnavailable."""
    load_axes, load_labels, available_categories = (loader or load_dictionary)(dict_root)
    fails, warns, notes = [], [], []

    category = card_category(card)
    if not category:
        raise DictionaryUnavailable(
            'card has no identity.category — cannot choose a dictionary to '
            'check it against')

    categories = list(available_categories())
    try:
        expected = set(load_axes(category))
        authored = {a: r.get('display') if isinstance(r, dict) else r
                    for a, r in load_labels(category).items()}
    except Exception as exc:
        raise DictionaryUnavailable(f'category {category!r} not in the dictionary: {exc}')

    shared = universal_axes(load_axes, categories)
    exclusive = expected - shared

    # 1. vocabulary
    axes = card_axes(card)
    if not axes:
        fails.append('no axes on the card at all — schema drift, check by hand')
    foreign = axes - expected
    if foreign:
        where = {}
        for other in categories:
            if other == category:
                continue
            for a in foreign & set(load_axes(other)):
                where.setdefault(a, []).append(other)
        detail = ', '.join(f'{a} ({"/".join(where[a])})' if a in where else a
                           for a in sorted(foreign))
        fails.append(f'axes foreign to {category}: {detail}')
    proof = axes & exclusive
    if not proof:
        fails.append(
            f'no axis exclusive to {category} — only shared axes present, so '
            f'the {category} dictionary was probably never loaded')
    else:
        notes.append(f'{len(proof)}/{len(exclusive)} {category}-exclusive axes present')

    # 2. labels
    for axis, display in sorted(card_labels(card).items()):
        if not display:
            fails.append(f'axis {axis} has no display_name')
        elif '.' in display or '_' in display:
            fails.append(f'raw axis id leaking as a label: {axis} -> "{display}"')
        elif axis in authored and display != authored[axis]:
            warns.append(f'label differs from the dictionary: {axis} -> "{display}" '
                         f'(authored: "{authored[axis]}")')

    # 3 & 4. freshness
    freshness = card.get('freshness') or {}
    created = freshness.get('created_at')
    if not created:
        fails.append('freshness.created_at missing')
    if 'staleness_days' in freshness:
        fails.append('staleness_days present — expected on any card published '
                     'before 2026-07-27, but a failure on a REBUILD: it means '
                     'the legacy writer produced this one')
    if published is not None:
        was = (published.get('freshness') or {}).get('created_at')
        if was and created and created != was:
            fails.append(f'mint date moved: {was} -> {created} — '
                         f'resolve_prior_created_at did not hold')
        elif was:
            notes.append(f'mint date preserved: {created}')
        else:
            warns.append('published card carries no created_at to compare against')
    if freshness.get('last_built') and created and freshness['last_built'] == created:
        warns.append('last_built == created_at — correct on a first mint, '
                     'wrong on a rebuild')

    # 5. grounding
    paragraph = (card.get('synthesis') or {}).get('consensus_paragraph') or ''
    if not paragraph:
        warns.append('no consensus paragraph')
    elif has_percentage(paragraph):
        triples = share_triples(paragraph)
        if not triples:
            fails.append('a sentiment share appears without its pos/neu/neg siblings')
        else:
            for t in triples:
                if sum(int(x) for x in t) not in (99, 100, 101):
                    warns.append(f'share triple does not sum to 100: {t}')
            notes.append(f'{len(triples)} grounded share triple(s)')
    low = paragraph.lower()
    for phrase in EXHAUSTIVE:
        if phrase in low:
            fails.append(f'exhaustiveness claim in prose: "{phrase}"')

    return fails, warns, notes


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('card', help='the rebuilt card JSON, as the factory wrote it')
    ap.add_argument('--published', help='the live card it will replace, for the '
                                        'mint-date comparison')
    ap.add_argument('--dict-root', default=DEFAULT_DICT_ROOT,
                    help=f'phantom-ops checkout (default: {DEFAULT_DICT_ROOT})')
    args = ap.parse_args(argv)

    card = json.loads(Path(args.card).read_text(encoding='utf-8'))
    published = None
    if args.published:
        published = json.loads(Path(args.published).read_text(encoding='utf-8'))

    try:
        fails, warns, notes = check(card, published, args.dict_root)
    except DictionaryUnavailable as exc:
        print(f'CANNOT CHECK: {exc}')
        print('Refusing to report a result. Fix the dictionary path and re-run.')
        return 2

    slug = (card.get('identity') or {}).get('slug') or card.get('card_id') or args.card
    print(f'card: {slug}   category: {card_category(card)}')
    for n in notes:
        print(f'  ok   {n}')
    for w in warns:
        print(f'  WARN {w}')
    for f in fails:
        print(f'  FAIL {f}')
    print()
    if fails:
        print(f'VERDICT: do NOT approve — {len(fails)} failure(s)')
        return 1
    tail = f' ({len(warns)} warning(s) to eyeball)' if warns else ''
    print(f'VERDICT: safe to approve{tail}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
