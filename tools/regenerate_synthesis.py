#!/usr/bin/env python3
"""
Regenerate freshness-neutral consensus paragraphs on published cards.
================================================================================
WHY
---
Every published card's `synthesis.consensus_paragraph` was written by a
generator that cited single sentiment shares: "524 claims, 32% positive" on an
axis that is 62% negative, "24% of 46 claims are negative" with no idea what
the other 76% was. Both synthesizers were corrected (classifier 2026-07-24,
lens 2026-07-27), but all 11 live cards were built between 06-19 and 07-23 and
so predate both fixes. The generators are right and the cards are fossils.

The paragraph is not decoration: build_site.py reuses it verbatim as the meta
description AND as the schema.org description, so one ungrounded sentence is
reaching three surfaces per card.

WHY IN PLACE, RATHER THAN A FULL RE-ASSEMBLE
--------------------------------------------
The paragraph is DERIVED from the card's own axis aggregates, which are not
changing. Verified before writing this: build_consensus_paragraph() run against
each stored envelope reproduces the same sentence selection and the same axis
roles as the stored text, differing only in the grounding fix. So regeneration
needs no corpus, no fetch, no enrich, and no model run.

That also settles the clocks, and it settles them differently than a full
re-assemble would. NOTHING IN `freshness` IS TOUCHED. last_built asserts that
the synthesis was recomputed; it was not. The sentiment classification, the
axis aggregates and the evidence are all identical — we fixed how we REPORT
them, not what we found. Moving last_built here would claim a fresh analysis
in order to publish a corrected sentence, which is the same species of
overclaim this whole change exists to remove.

SAFETY
------
  * Dry-run by default; --commit writes.
  * An empty regenerated paragraph is REFUSED, never written. A card whose
    axes cannot produce prose keeps the prose it has; fabricating nothing is
    the standing rule, and blanking a live card would be worse than a stale
    sentence.
  * Unchanged output is reported as unchanged rather than rewritten, so a
    re-run is a no-op and the diff stays honest.
  * Byte-exact serialization (indent=2, ensure_ascii=False, no trailing
    newline), so each card is a one-line diff.

USAGE
  python3 tools/regenerate_synthesis.py                    # dry-run table
  python3 tools/regenerate_synthesis.py --card sony-a7iv   # one card
  python3 tools/regenerate_synthesis.py --commit           # write
  python3 tools/regenerate_synthesis.py --synthesizer-path <aggregator-build>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CARDS_DIR = ROOT / 'data' / 'cards'

# The synthesizer lives in the phantom-ops workspace, not this repo. The box has
# both checked out; the path is overridable so tests and sandboxes never depend
# on the box layout. Same seam, and same reasoning, as card_factory's
# DEFAULT_BUILD_CARD.
DEFAULT_SYNTHESIZER = Path('/home/phantomops/phantom-ops/claude/workspace'
                           '/aggregator-build')

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_NO_SYNTHESIZER = 3


def load_builder(path):
    """Import build_consensus_paragraph from the aggregator workspace.

    Fails LOUDLY rather than degrading: a missing synthesizer means we cannot
    regenerate, and silently leaving every card untouched while reporting
    success is exactly the kind of quiet no-op that hides for weeks."""
    path = Path(path)
    if not (path / 'synthesize_classifier.py').exists():
        return None
    sys.path.insert(0, str(path))
    from synthesize_classifier import build_consensus_paragraph
    return build_consensus_paragraph


def regenerate(card, builder):
    """(new_paragraph_or_None, reason). None means: do not write."""
    stored = (card.get('synthesis') or {}).get('consensus_paragraph') or ''
    try:
        fresh = builder(card) or ''
    except Exception as exc:                      # noqa: BLE001
        return None, f'generator raised {type(exc).__name__}: {exc}'
    fresh = fresh.strip()
    if not fresh:
        return None, 'generator produced nothing — keeping existing prose'
    if fresh == stored.strip():
        return None, 'already current'
    return fresh, 'regenerated'


def _grounded(text):
    """Does the text bring all three polarities, or none?

    Deliberately NOT keyed to the "N% positive" adjacency pattern. The first
    version of this check was, and it silently passed "24% of 46 claims are
    negative" — the exact old gripe sentence it existed to catch — because the
    share and its polarity word sit at opposite ends of the clause. It also
    passed "41 positive vs 55 negative claims", which drops neutral while using
    counts rather than shares.

    So the rule is stated on the polarity WORDS: mention one, mention all
    three. That catches share forms, count forms and prose forms alike, and a
    check meant to catch under-reporting must not itself under-report."""
    import re
    found = {m.group(0) for m in
             re.finditer(r'positive|neutral|negative', text)}
    return (not found) or found == {'positive', 'neutral', 'negative'}


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='regenerate_synthesis.py',
        description='Rewrite consensus paragraphs from unchanged axis data.')
    ap.add_argument('--card', default=None)
    ap.add_argument('--commit', action='store_true')
    ap.add_argument('--cards-dir', default=str(CARDS_DIR))
    ap.add_argument('--synthesizer-path', default=str(DEFAULT_SYNTHESIZER))
    args = ap.parse_args(argv)

    builder = load_builder(args.synthesizer_path)
    if builder is None:
        print(f'FATAL: no synthesizer at {args.synthesizer_path} — cannot '
              f'regenerate. (Pass --synthesizer-path to the aggregator-build '
              f'directory.)', file=sys.stderr)
        return EXIT_NO_SYNTHESIZER

    cards_dir = Path(args.cards_dir)
    paths = ([cards_dir / f'{args.card}.json'] if args.card
             else sorted(cards_dir.glob('*.json')))

    changed = unchanged = refused = 0
    for path in paths:
        if not path.exists():
            print(f'{path.stem:<27} card not found at {path}')
            refused += 1
            continue
        card = json.loads(path.read_text(encoding='utf-8'))
        fresh, reason = regenerate(card, builder)
        if fresh is None:
            if reason == 'already current':
                print(f'{path.stem:<27} unchanged')
                unchanged += 1
            else:
                print(f'{path.stem:<27} REFUSED: {reason}')
                refused += 1
            continue

        flag = '' if _grounded(fresh) else '   [!] STILL UNGROUNDED'
        print(f'{path.stem:<27} regenerated{flag}')
        print(f'    was: {(card.get("synthesis") or {}).get("consensus_paragraph", "")[:96]}')
        print(f'    now: {fresh[:96]}')
        changed += 1

        if args.commit:
            synthesis = card.get('synthesis') or {}
            synthesis['consensus_paragraph'] = fresh
            card['synthesis'] = synthesis
            # freshness is deliberately UNTOUCHED — see the module docstring.
            path.write_text(json.dumps(card, indent=2, ensure_ascii=False),
                            encoding='utf-8')

    verb = 'regenerated' if args.commit else 'would regenerate'
    print(f'\n{verb}: {changed}   unchanged: {unchanged}   refused: {refused}')
    if not args.commit and changed:
        print('Dry-run — nothing written. Re-run with --commit to apply.')
    return EXIT_REFUSED if refused else EXIT_OK


if __name__ == '__main__':
    raise SystemExit(main())
