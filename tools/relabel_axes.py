#!/usr/bin/env python3
"""Bring a card's axis headings into line with the authored dictionary.

Some published cards carry display names that predate the LABELS facet added
to the dictionary on 2026-07-27: sony-a7iv renders "Evf Lcd", "Burst Buffer"
and "Autofocus Body" where the dictionary now authors "EVF & LCD", "Burst &
Buffer" and "Autofocus". The axes are right. Only the names a reader sees are
stale.

WHY THIS IS A RELABEL AND NOT A REBUILD

Rebuilding a7iv would move last_built, and last_built asserts that synthesis
was recomputed. Recomputing 55 sources to improve three headings would make
that assertion false — the same freshness overclaim ruled out on 2026-07-27
when the consensus paragraphs were regenerated in place rather than rebuilt.
So this edits the field and nothing else. A test pins the clocks.

WHAT IT REFUSES

A card carrying axes foreign to its own category cannot be repaired here, and
saying so is most of this tool's value. The seven bodies extracted with the
lens dictionary have no fixable labels: there is no correct body name for
"optical performance", because the axis itself is wrong. Looking it up in the
body dictionary misses, and a tool that quietly skipped those misses would
report "0 changes" on a card that badly needs re-extraction — indistinguishable
from a card that was already correct. Those cards get named and pointed at
tools/requeue_rebuild.py instead.

An axis with no authored label is likewise left alone rather than blanked.

2026-11-18: the bare `af_performance` once called "no longer a dictionary
axis" is the lens card's composite axis over af_performance.stills/.video. It
is allowed as the parent of those dotted axes (verify_rebuilt_card
with_composites) and phantom-ops now authors its label, "Autofocus" — 106 of
129 lens cards stored the raw id and five lens guides put it in their meta
description. `--queue` extends the run to the assembled cards waiting at
/admin/publish, so a rebuild is fixed before approval rather than after.

    python3 tools/relabel_axes.py --slug sony-a7iv
    python3 tools/relabel_axes.py --slug sony-a7iv --apply
    python3 tools/relabel_axes.py --all
    python3 tools/relabel_axes.py --all --queue data/work_queue.json [--apply]

Dry run is the default. After --apply, rebuild the site: display_name reaches
both the card page and cards-manifest.json, and data/cards is not the deploy.

Exit 0 = done (or nothing to do). Exit 1 = a card needs re-extraction.
Exit 2 = could not check.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_rebuilt_card import (DictionaryUnavailable, DEFAULT_DICT_ROOT,  # noqa: E402
                                 card_axes, card_category, load_dictionary,
                                 with_composites)

CARDS_DIR = Path(__file__).resolve().parent.parent / 'data' / 'cards'

# Every block that must survive a relabel untouched. freshness is the one that
# matters — see the docstring — but the others are here so a future edit to
# this tool has to be deliberate about widening its blast radius.
UNTOUCHED = ('freshness', 'pricing', 'sources', 'facts', 'confidence',
             'synthesis', 'identity')


def relabel(card, load_axes, load_labels):
    """Return (changes, blocked). Mutates `card` in place.

    changes: list of (axis_id, before, after)
    blocked: list of reasons this card cannot be relabelled at all
    """
    category = card_category(card)
    if not category:
        raise DictionaryUnavailable('card has no identity.category')
    try:
        expected = with_composites(load_axes(category))
        authored = {a: r.get('display') if isinstance(r, dict) else r
                    for a, r in load_labels(category).items()}
    except Exception as exc:
        raise DictionaryUnavailable(f'category {category!r} not loadable: {exc}')

    foreign = card_axes(card) - expected
    if foreign:
        return [], [f'carries axes foreign to {category} ({", ".join(sorted(foreign))}) '
                    f'— this needs re-extraction, not a relabel: '
                    f'tools/requeue_rebuild.py']

    changes = []
    for key in ('lead_axes', 'detail_axes'):
        for axis in card.get(key) or []:
            if not isinstance(axis, dict):
                continue
            axis_id = axis.get('axis_id')
            want = authored.get(axis_id)
            if not axis_id or not want:
                continue  # no authored label — leave it, never blank it
            have = axis.get('display_name')
            if have != want:
                changes.append((axis_id, have, want))
                axis['display_name'] = want
    return changes, []


def _fingerprint(card):
    """The blocks a relabel must not disturb, as comparable JSON."""
    return {k: json.dumps(card.get(k), sort_keys=True) for k in UNTOUCHED}


def process(path, load_axes, load_labels, apply=False):
    original = json.loads(Path(path).read_text(encoding='utf-8'))
    card = json.loads(json.dumps(original))
    before = _fingerprint(card)

    changes, blocked = relabel(card, load_axes, load_labels)

    after = _fingerprint(card)
    disturbed = [k for k in UNTOUCHED if before[k] != after[k]]
    if disturbed:
        raise RuntimeError(
            f'relabel disturbed {disturbed} on {path} — refusing to write. '
            f'This is a bug in this tool, not in the card.')

    if changes and apply:
        # Byte-exact serialization, matching regenerate_synthesis.py:
        # indent=2, ensure_ascii=False, no trailing newline. An in-place edit
        # should produce a diff of exactly the lines it meant to change.
        Path(path).write_text(json.dumps(card, indent=2, ensure_ascii=False),
                              encoding='utf-8')
    return changes, blocked


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--slug', action='append', default=[],
                    help='card slug; repeatable')
    ap.add_argument('--all', action='store_true', help='every card in data/cards')
    ap.add_argument('--apply', action='store_true',
                    help='write the changes (default is a dry run)')
    ap.add_argument('--queue', help='work_queue.json: also the review_ready '
                                    'cards waiting at /admin/publish')
    ap.add_argument('--cards-dir', default=str(CARDS_DIR))
    ap.add_argument('--dict-root', default=DEFAULT_DICT_ROOT)
    args = ap.parse_args(argv)

    cards_dir = Path(args.cards_dir)
    if args.all:
        paths = sorted(cards_dir.glob('*.json'))
    elif args.slug:
        paths = [cards_dir / f'{s}.json' for s in args.slug]
    elif not args.queue:
        ap.error('pass --slug, --all or --queue')
    else:
        paths = []
    if args.queue:
        queue = json.loads(Path(args.queue).read_text(encoding='utf-8')).get('queue', {})
        paths += [Path(r['card_path']) for _, r in sorted(queue.items())
                  if r.get('state') == 'review_ready' and r.get('card_path')]

    try:
        load_axes, load_labels, _ = load_dictionary(args.dict_root)
    except DictionaryUnavailable as exc:
        print(f'CANNOT CHECK: {exc}')
        return 2

    total, needs_rebuild = 0, []
    for path in paths:
        name = path.parent.name if path.name == 'card.json' else path.stem
        if not path.exists():
            print(f'{name}: no such card')
            return 2
        try:
            changes, blocked = process(path, load_axes, load_labels, args.apply)
        except DictionaryUnavailable as exc:
            print(f'{name}: CANNOT CHECK — {exc}')
            return 2
        if blocked:
            needs_rebuild.append(path.stem)
            for reason in blocked:
                print(f'{name}: BLOCKED — {reason}')
        elif changes:
            total += len(changes)
            verb = 'relabelled' if args.apply else 'would relabel'
            for axis_id, was, now in changes:
                print(f'{name}: {verb} {axis_id}: "{was}" -> "{now}"')
        else:
            print(f'{name}: labels already match the dictionary')

    print()
    if args.apply and total:
        print(f'{total} label(s) written. data/cards is NOT the deploy — rebuild '
              f'the site next:')
        print('  python3 tools/build_site.py --cards-dir data/cards '
              '--output-dir browser --manifest')
    elif total:
        print(f'{total} label(s) would change. Re-run with --apply.')
    if needs_rebuild:
        print(f'{len(needs_rebuild)} card(s) need re-extraction, not a relabel: '
              f'{", ".join(needs_rebuild)}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
