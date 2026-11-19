#!/usr/bin/env python3
"""Re-point one spine entry's contamination_key to a verified registry product.

Operator front for skus_registry.correct_contamination_key (compare-and-swap,
receipt in contamination_key_provenance, carried through a listing rotation).
A card minted before its specific registry entry existed keeps the brand-level
key it was minted with; the relevance gate then runs at brand precision for good.
This moves it to the entry that names the product.

Dry run by default: prints the current key, the target's registry record and the
card's name, so the target can be checked as the same product before --apply.

The registry lives in the phantom-ops checkout (0700 phantomops on the box), and
the spine must be written as askmaddi (its owner; an atomic replace as root would
take the file away from the gateway). So on the box root reads the registry and
pipes it in:

    cat /home/phantomops/phantom-ops/claude/workspace/aggregator-build/fixtures/manifests/contamination.json \\
      | sudo -u askmaddi python3 tools/correct_contamination_key.py --contam-json /dev/stdin \\
          --slug sony-fe-35mm-f1-4-gm --expect sony-generic --new sony-35-f14-gm \\
          --reason "first-class registry entry exists for this lens"

Add --apply to write. Exit 0 written or would write, 1 refused, 2 bad input.
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'gateway'))
import skus_registry  # noqa: E402

REFUSALS = {'stale-expect', 'no-op', 'missing-slug', 'unknown-key', 'registry-unreadable'}


def main(argv=None, out=sys.stdout):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--slug', required=True)
    ap.add_argument('--expect', required=True, help='the key the spine holds now')
    ap.add_argument('--new', required=True, help='the registry product to point at')
    ap.add_argument('--reason', required=True)
    ap.add_argument('--actor', default='lee')
    ap.add_argument('--contam-json', default=None,
                    help='contamination.json (/dev/stdin to pipe it in)')
    ap.add_argument('--skus', default=str(skus_registry.SKUS_PATH))
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args(argv)

    # Read the registry ONCE (stdin can only be read once), then hand the door a
    # private copy so the dry-run report and the write see the same bytes.
    try:
        raw = Path(a.contam_json).read_text(encoding='utf-8') if a.contam_json else \
            json.dumps({'products': skus_registry._load_contamination_products()})
        products = json.loads(raw).get('products')
    except (OSError, ValueError, AttributeError):
        products = None
    if not products:
        print('REFUSED: contamination registry unreadable — pass --contam-json', file=out)
        return 1

    spine = skus_registry.load_registry(a.skus).get('skus', {})
    entry = spine.get(a.slug)
    if entry is None:
        print(f'REFUSED: no spine entry {a.slug!r}', file=out)
        return 1
    current = entry.get('contamination_key')
    print(f'{a.slug}: contamination_key {current!r} -> {a.new!r}', file=out)
    print(f'  spine     : vendor {entry.get("vendor")!r}  model {entry.get("model")!r}  '
          f'mpn {(entry.get("identity") or {}).get("mpn")!r}', file=out)
    target = products.get(a.new)
    if target is None:
        print(f'REFUSED: {a.new!r} is not a product in the registry', file=out)
        return 1
    print(f'  target    : vendor {target.get("vendor")!r}  model {target.get("model")!r}  '
          f'self {((target.get("self") or {}).get("aliases") or [])[:6]}', file=out)

    if current != a.expect:
        print(f'REFUSED: spine holds {current!r}, not the expected {a.expect!r}', file=out)
        return 1
    if not a.apply:
        print('dry run — nothing written; add --apply', file=out)
        return 0

    with tempfile.TemporaryDirectory() as d:
        cp = Path(d) / 'contamination.json'
        cp.write_text(raw, encoding='utf-8')
        res = skus_registry.correct_contamination_key(
            a.slug, a.new,
            {'method': 'correct_contamination_key', 'actor': a.actor, 'reason': a.reason},
            expect_key=a.expect, contam_path=cp, path=a.skus)
    if res in REFUSALS:
        print(f'REFUSED: {res}', file=out)
        return 1
    after = skus_registry.load_registry(a.skus)['skus'][a.slug]
    print(f'{res}: now {after.get("contamination_key")!r}  tier '
          f'{after.get("contamination_tier")!r}', file=out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
