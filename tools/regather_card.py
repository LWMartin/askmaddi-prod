#!/usr/bin/env python3
"""Re-open a card for a full corpus RE-GATHER, with fetch inputs re-derived.

The operator action for "this card's corpus is about the wrong product." Not
the same as tools/requeue_rebuild.py, which resumes at `extract` and re-reads
the SAME cached triples with current gates — the right move when a gate
changed, and useless when the evidence pool itself is wrong.

WHY THE FETCH INPUTS MUST BE RE-DERIVED, NOT JUST THE STAGE MOVED

`card_factory` passes the work_queue record's `label` and `aliases` straight
through to build_card, which forwards them to run_fetch_only as --sku-label
and --alias, and builds the YouTube query as "<label> review". Those values
are FROZEN at enroll() time. So a re-gather that only rewinds the stage
re-fetches against the original strings and reproduces the original corpus.

Nothing else propagates: correcting the spine, or the card identity via
`overrides`, does not reach the queue record. This seam has to be written.

WHERE THE STRINGS COME FROM

Both from the contamination registry entry, never hardcoded here. That file
already declares what strings name a product (`self.aliases`) and is what the
relevance gate uses to judge a source on-target. Deriving the fetch inputs
from the same place means the thing that decides what gets FETCHED and the
thing that decides what counts as ON-TARGET cannot drift apart — which is
exactly how sony-a7r came to hold a corpus of A7R V reviews: its registry
entry listed "a7r v" as a self alias, so those sources were on-target by
declaration.

Fix the registry entry first. This tool then carries it to the queue.

ORDER IS FORCED

`set_aliases` refuses any record not at `resolved`, so the record must be
re-opened BEFORE it can be relabelled. A promoted (live) card re-opens via
requeue_promoted; a terminal one via requeue. Both are done here in order,
and a record in flight (`building`) or awaiting a human (`review_ready`) is
refused rather than yanked.

THE LIVE CARD IS NOT TOUCHED. Only the queue record moves. The rebuilt card
replaces the published one only after the human gate approves at
/admin/publish.

MUST RUN ON THE BOX. data/work_queue.json is gitignored runtime state; it
does not exist in a repo clone and cannot ship through git. Run as the
service account:

    sudo -u phantomops python3 tools/regather_card.py --slug sony-a7r
    sudo -u phantomops python3 tools/regather_card.py --slug sony-a7r --apply

Dry run is the default.

Exit 0 = done. Exit 1 = record not in a re-openable state.
Exit 2 = could not check (registry unavailable, or it disagrees).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway import work_queue  # noqa: E402

DEFAULT_AGGREGATOR_ROOT = (
    Path.home() / 'phantom-ops' / 'claude' / 'workspace' / 'aggregator-build')

CONTAMINATION_RELPATH = Path('fixtures') / 'manifests' / 'contamination.json'

# States this tool knows how to re-open, and how.
_VIA_REQUEUE_PROMOTED = ('promoted',)
_VIA_REQUEUE = ('failed', 'rejected', 'corpus_thin', 'needs_category')
_ALREADY_OPEN = ('resolved',)


class RegistryUnavailable(Exception):
    """The contamination registry could not be read.

    Refusing beats guessing: without it there is no authored answer for what
    strings name this product, and inventing one here would re-create the
    two-declarations problem this tool exists to avoid.
    """


def load_entry(aggregator_root, slug):
    """The contamination registry entry for a slug, or raise."""
    path = Path(aggregator_root) / CONTAMINATION_RELPATH
    if not path.exists():
        raise RegistryUnavailable(f'no contamination.json at {path}')
    products = (json.loads(path.read_text()).get('products') or {})
    entry = products.get(slug)
    if entry is None:
        raise RegistryUnavailable(
            f'{slug} has no entry in {path.name} — author one before '
            f're-gathering, or the fetch has nothing authored to aim at')
    return entry


def fetch_inputs(entry, slug):
    """Derive (label, aliases) for the fetch seam from a registry entry.

    `self.aliases` may be the literal string "auto", meaning the gate derives
    them from vendor+model at load time. That derivation lives in
    classifier_extract and is not importable here, so "auto" is refused rather
    than approximated — an approximation would silently differ from what the
    gate actually uses, which is the failure mode being fixed.
    """
    vendor = (entry.get('vendor') or '').strip()
    model = (entry.get('model') or '').strip()
    if not vendor or not model:
        raise RegistryUnavailable(
            f'{slug}: registry entry lacks vendor/model, cannot build a label')
    aliases = (entry.get('self') or {}).get('aliases')
    if aliases == 'auto':
        raise RegistryUnavailable(
            f'{slug}: self.aliases is "auto" — the gate derives these at load '
            f'time via classifier_extract._product_aliases, which this tool '
            f'cannot reproduce faithfully. Author explicit aliases first.')
    if not isinstance(aliases, list) or not aliases:
        raise RegistryUnavailable(f'{slug}: no explicit self.aliases to derive')
    return f'{vendor} {model}', [str(a).strip() for a in aliases if str(a).strip()]


def plan_for(record, slug):
    """Return (how, reason). `how` is None when the record cannot be re-opened."""
    if record is None:
        return None, f'{slug} is not in the work queue'
    state = record.get('state')
    if state in _ALREADY_OPEN:
        return 'already-open', f'already at {state}; only the fetch inputs change'
    if state in _VIA_REQUEUE_PROMOTED:
        return 'requeue-promoted', f'live card at {state}; re-open for rebuild'
    if state in _VIA_REQUEUE:
        return 'requeue', f'terminal at {state}; re-open'
    return None, (f'state {state!r} is in flight or awaiting a human — '
                  f'let it finish or adjudicate it, do not yank it')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--slug', required=True)
    ap.add_argument('--apply', action='store_true',
                    help='write the change (default is a dry run)')
    ap.add_argument('--aggregator-root', default=str(DEFAULT_AGGREGATOR_ROOT))
    ap.add_argument('--queue-path', default=None, help='override (tests)')
    args = ap.parse_args(argv)

    path = Path(args.queue_path) if args.queue_path else work_queue.WORK_QUEUE_PATH

    try:
        entry = load_entry(args.aggregator_root, args.slug)
        label, aliases = fetch_inputs(entry, args.slug)
    except RegistryUnavailable as exc:
        print(f'REFUSED: {exc}', file=sys.stderr)
        return 2

    record = work_queue.get(args.slug, path=path)
    how, reason = plan_for(record, args.slug)
    if how is None:
        print(f'REFUSED: {reason}', file=sys.stderr)
        return 1

    before_label = (record or {}).get('label')
    before_aliases = (record or {}).get('aliases') or []
    print(f'{args.slug}: {reason}')
    print(f'  label   : {before_label!r} -> {label!r}')
    print(f'  aliases : {len(before_aliases)} -> {len(aliases)}  {aliases}')
    print(f'  stage   : full re-gather (start at fetch, not extract)')

    if not args.apply:
        print('\n(dry run — pass --apply to write)')
        return 0

    if how == 'requeue-promoted':
        work_queue.requeue_promoted(args.slug, resume_stage='fetch', path=path)
    elif how == 'requeue':
        work_queue.requeue(args.slug, path=path)

    status = work_queue.set_aliases(args.slug, aliases, label=label, path=path)
    if status != 'set':
        print(f'REFUSED: set_aliases returned {status!r} — the record was '
              f're-opened but NOT relabelled, so a build now would re-fetch '
              f'against the old strings. Re-run before letting the drip claim '
              f'it.', file=sys.stderr)
        return 1

    print('\nre-gathered inputs written; the drip will claim it in FIFO order.')
    print('NOTE: a tightened alias set gathers FEWER sources. If the corpus '
          'lands under the floor the build parks `corpus_thin` rather than '
          'shipping a thin card — that is the honest outcome, not a failure.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
