#!/usr/bin/env python3
"""Stamp the axis_roles block onto published cards, in place.

New cards get the block from the assembler. Published cards predate it, and
until they carry it build_site falls back to recomputing the selection here --
a second computation of one decision, which is the pattern that produced the
"biggest_gripe" mislabel. Rather than wait for the drip to turn the catalog
over, derive the block now from the axes already on each card.

WHY THIS IS SAFE TO DO IN PLACE

The block is a pure function of lead_axes/detail_axes, which this tool does
not touch. Nothing is re-analyzed, no model runs, no source is fetched. It
records a decision the renderer was already making at render time, so on a
correct card the rendered output does not change at all -- and a test in
test_build_site.py asserts exactly that equivalence across the corpus.

That is also why the freshness clocks are left alone. last_built asserts that
synthesis was recomputed; recording a selection that was already being made
recomputes nothing, and moving the clock for it would be the overclaim ruled
out on 2026-07-27 when the consensus paragraphs were regenerated.

    python3 tools/stamp_axis_roles.py
    python3 tools/stamp_axis_roles.py --commit
    python3 tools/stamp_axis_roles.py --card sony-a7iv --commit

Dry run is the default. Exit 0 = done, 2 = could not reach the assembler.
"""
import argparse
import json
import sys
from pathlib import Path

CARDS_DIR = Path(__file__).resolve().parent.parent / 'data' / 'cards'
DEFAULT_ASSEMBLER = Path('/home/phantomops/phantom-ops/claude/workspace'
                         '/aggregator-build')

# Blocks a role stamp must never disturb. freshness is the one that matters;
# the others are here so a future edit has to be deliberate about widening.
UNTOUCHED = ('freshness', 'pricing', 'sources', 'facts', 'confidence',
             'synthesis', 'identity', 'lead_axes', 'detail_axes')


def load_stamper(path):
    p = Path(path)
    if not p.is_dir():
        raise RuntimeError(f'no aggregator-build at {p} — pass --assembler-path')
    sys.path.insert(0, str(p))
    from assemble_card import _stamp_axis_roles
    from card_envelope import empty_axis_roles
    return _stamp_axis_roles, empty_axis_roles


def process(path, stamp, empty, commit=False):
    original = json.loads(Path(path).read_text(encoding='utf-8'))
    card = json.loads(json.dumps(original))
    before = {k: json.dumps(card.get(k), sort_keys=True) for k in UNTOUCHED}

    was = card.get('axis_roles')
    card.setdefault('axis_roles', empty())
    stamp(card)
    now = card['axis_roles']

    after = {k: json.dumps(card.get(k), sort_keys=True) for k in UNTOUCHED}
    disturbed = [k for k in UNTOUCHED if before[k] != after[k]]
    if disturbed:
        raise RuntimeError(f'stamp disturbed {disturbed} on {path} — refusing '
                           f'to write. This is a bug in this tool.')

    changed = was != now
    if changed and commit:
        # Byte-exact with regenerate_synthesis.py and relabel_axes.py:
        # indent=2, ensure_ascii=False, no trailing newline.
        Path(path).write_text(json.dumps(card, indent=2, ensure_ascii=False),
                              encoding='utf-8')
    return changed, now


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--card', action='append', default=[])
    ap.add_argument('--commit', action='store_true')
    ap.add_argument('--cards-dir', default=str(CARDS_DIR))
    ap.add_argument('--assembler-path', default=str(DEFAULT_ASSEMBLER))
    args = ap.parse_args(argv)

    try:
        stamp, empty = load_stamper(args.assembler_path)
    except Exception as exc:
        print(f'CANNOT STAMP: {exc}')
        return 2

    cards_dir = Path(args.cards_dir)
    paths = ([cards_dir / f'{c}.json' for c in args.card] if args.card
             else sorted(cards_dir.glob('*.json')))

    changed = 0
    for path in paths:
        if not path.exists():
            print(f'{path.stem}: no such card')
            return 2
        did, block = process(path, stamp, empty, args.commit)
        roles = ' '.join(f'{k}={block[k]}' for k in
                         ('most_discussed', 'highest_rated', 'lowest_rated'))
        print(f'{path.stem:28} {"stamped" if did else "unchanged":10} {roles}')
        changed += bool(did)

    print()
    if changed and not args.commit:
        print(f'{changed} card(s) would change. Re-run with --commit.')
    elif changed:
        print(f'{changed} card(s) stamped. The rendered teaser is unchanged by '
              f'construction — rebuild the site to pick up the block:')
        print('  python3 tools/build_site.py --cards-dir data/cards '
              '--output-dir browser --manifest')
    return 0


if __name__ == '__main__':
    sys.exit(main())
