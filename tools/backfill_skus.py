#!/usr/bin/env python3
"""
Back-fill skus.json from existing card identity facts (registry Stage 1, step 3).
================================================================================
Resolves each existing card against eBay ONCE to populate its lossless identity
block, authoring the first data/skus.json. Per maddi-skus-registry:

  - Frozen slugs read as FACTS (decision #6) — never slugify, never rename.
  - Auto-pick WITH a confidence gate (operator chose option b): score candidates
    on brand+model+alt-name token overlap; take the top hit only if it clears
    the threshold, else mark needs_review rather than seeding a wrong identity.
  - DRY-RUN by default: writes a review artifact (per card: chosen candidate,
    score, rejected alternatives, gate decision). Nothing is written to
    skus.json until --commit. The operator's final deploy-review is the human
    backstop on the seed cadre.
  - Gemma escalation (--gemma) is an OPTIONAL tier for ambiguous cases, off by
    default so the back-fill runs without the shim up (loopback :5101 /orient).

Run on the box (or local) where eBay creds live in gateway/.env — this calls
the live Browse API. Sandbox has no creds; --dry-run --offline-demo shows shape.

Usage:
  python3 tools/backfill_skus.py                    # dry-run, all cards, review only
  python3 tools/backfill_skus.py --card sony-a7iv   # one card
  python3 tools/backfill_skus.py --commit           # write entries that pass the gate
  python3 tools/backfill_skus.py --gemma            # escalate ambiguous picks to Gemma
"""
import argparse
import json
import re
import sys
from pathlib import Path

# gateway/ is the home of ebay_api + skus_registry; add it to the path.
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / 'gateway'))

import ebay_api          # noqa: E402
import skus_registry     # noqa: E402

CARDS_DIR = ROOT / 'data' / 'cards'

# Confidence gate: min token-overlap score (0..1) to auto-accept a candidate.
# Below this -> needs_review (or Gemma escalation if --gemma).
ACCEPT_THRESHOLD = 0.60

# contamination_key bridge: prod slug -> hyphenated editorial key. The two
# differ in form by design (spec decision #6); this is the explicit mapping,
# not a generated transform. Seed cadre only; extend as cards are added.
CONTAMINATION_KEY = {
    'sony-a7iv': 'sony-a7-iv',
    'sigma-35-art-dg-dn-ii': 'sigma-35-dg-dn-art-ii',
    'peak-design-travel-tripod': 'peak-design-travel-tripod',
    'peak-design-pro-tripod': 'peak-design-pro-tripod',
}

# Category back-fill for cards missing it (flagged, not silently authoritative).
# sigma lens card carries category=None; lens is the correct controlled value.
CATEGORY_DEFAULT = {'sigma-35-art-dg-dn-ii': 'lens'}

_TOKEN_RE = re.compile(r'[a-z0-9]+')


def _tokens(text):
    return set(_TOKEN_RE.findall((text or '').lower()))


def _score(card_idn, candidate_title):
    """Token-overlap score (0..1) of a candidate title vs the card's identity.

    Uses brand + model + alt-names as the reference token set — the card's
    own facts, which are richer than display_name alone. Score is the fraction
    of reference tokens present in the candidate title (recall-weighted: we
    care that the candidate contains the product's identifying tokens, not that
    it's free of extra ones — eBay titles are keyword-stuffed).
    """
    ref = set()
    ref |= _tokens(card_idn.get('brand'))
    ref |= _tokens(card_idn.get('model'))
    for alt in (card_idn.get('sku_alt_names') or []):
        ref |= _tokens(alt)
    # Drop pure noise tokens that match everything.
    ref -= {'the', 'a', 'an', 'for', 'with', 'and', 'mm', 'f'}
    if not ref:
        return 0.0
    cand = _tokens(candidate_title)
    hit = ref & cand
    return len(hit) / len(ref)


def _gemma_adjudicate(card_idn, candidates):
    """Optional Gemma escalation for ambiguous picks (loopback shim :5101).

    Off unless --gemma. Returns the chosen item_id or None. Kept deliberately
    thin — a hook, not a dependency; if the shim is down it returns None and
    the card falls to needs_review rather than erroring the whole back-fill.
    """
    try:
        import requests
        prompt = {
            'product': {
                'brand': card_idn.get('brand'),
                'model': card_idn.get('model'),
                'display_name': card_idn.get('display_name'),
            },
            'candidates': [{'item_id': c['item_id'], 'title': c['title']} for c in candidates],
            'task': 'pick the single candidate that is the SAME product, or null if none',
        }
        r = requests.post('http://127.0.0.1:5101/orient',
                          json=prompt, timeout=30)
        if r.status_code == 200:
            return (r.json() or {}).get('item_id')
    except Exception as e:
        print(f'  [gemma] escalation unavailable ({e}); falling to needs_review')
    return None


def backfill_card(slug, card, use_gemma=False, limit=10):
    """Resolve one card -> proposed skus entry + a review record. No writes."""
    idn = card.get('identity', {})
    display = idn.get('display_name', slug)
    review = {'slug': slug, 'query': display, 'decision': None,
              'chosen': None, 'score': 0.0, 'rejected': [], 'gemma_used': False}

    candidates = ebay_api._search_candidates(display, limit=limit)
    if not candidates:
        review['decision'] = 'no_candidates'
        return None, review

    scored = sorted(
        ((_score(idn, c['title']), c) for c in candidates),
        key=lambda t: t[0], reverse=True,
    )
    top_score, top = scored[0]
    review['rejected'] = [
        {'title': c['title'][:80], 'score': round(s, 3)} for s, c in scored[1:5]
    ]

    chosen = None
    if top_score >= ACCEPT_THRESHOLD:
        chosen = top
        review['decision'] = 'auto_accept'
    elif use_gemma:
        gid = _gemma_adjudicate(idn, [c for _, c in scored[:5]])
        review['gemma_used'] = True
        if gid:
            chosen = next((c for _, c in scored if c['item_id'] == gid), None)
            review['decision'] = 'gemma_accept' if chosen else 'gemma_no_match'
        else:
            review['decision'] = 'gemma_no_match'
    else:
        review['decision'] = 'needs_review'

    review['score'] = round(top_score, 3)
    if chosen is None:
        review['chosen'] = {'title': top['title'][:80], 'item_id': top['item_id']}
        return None, review

    review['chosen'] = {'title': chosen['title'][:80], 'item_id': chosen['item_id']}

    # Resolve the chosen item for lossless identity, customid = the frozen slug.
    resolved = ebay_api.resolve(chosen['item_id'], customid=slug)
    category = card.get('category') or CATEGORY_DEFAULT.get(slug)
    if card.get('category') is None and slug in CATEGORY_DEFAULT:
        review['category_backfilled'] = category
    entry = skus_registry.build_entry(
        slug=slug,
        vendor=idn.get('brand') or idn.get('vendor') or '',
        model=idn.get('model') or '',
        category=category,
        contamination_key=CONTAMINATION_KEY.get(slug, slug),
        resolved=resolved,
    )
    return entry, review


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--card', help='back-fill a single slug (default: all)')
    ap.add_argument('--commit', action='store_true',
                    help='write gate-passing entries to skus.json (default: dry-run)')
    ap.add_argument('--gemma', action='store_true',
                    help='escalate ambiguous picks to the Gemma shim (:5101)')
    ap.add_argument('--limit', type=int, default=10)
    args = ap.parse_args()

    slugs = ([args.card] if args.card
             else sorted(p.stem for p in CARDS_DIR.glob('*.json')))

    results = []
    for slug in slugs:
        card_path = CARDS_DIR / f'{slug}.json'
        if not card_path.exists():
            print(f'[skip] {slug}: no card file')
            continue
        card = json.loads(card_path.read_text(encoding='utf-8'))
        print(f'\n=== {slug} ===')
        try:
            entry, review = backfill_card(slug, card, use_gemma=args.gemma, limit=args.limit)
        except ebay_api.EbayAPIError as e:
            print(f'  eBay API error: {e}')
            if 'not set' in str(e):
                print('  -> run on the box (or local) where gateway/.env has EBAY_* creds.')
            results.append({'slug': slug, 'decision': 'api_error', 'score': 0.0,
                            'chosen': None, 'rejected': []})
            continue
        print(f"  decision: {review['decision']}  score: {review['score']}")
        if review['chosen']:
            print(f"  chosen:   {review['chosen']['title']}  [{review['chosen']['item_id']}]")
        for rej in review['rejected']:
            print(f"    rejected ({rej['score']}): {rej['title']}")
        if review.get('category_backfilled'):
            print(f"  NOTE: category back-filled to '{review['category_backfilled']}' (card had none)")

        if entry and args.commit:
            status = skus_registry.upsert(slug, entry)
            print(f"  WROTE skus.json: {status}")
        elif entry:
            print('  (dry-run — entry ready, not written; pass --commit to write)')

        results.append(review)

    # Review summary for the operator's final OK.
    accepted = [r for r in results if r['decision'] in ('auto_accept', 'gemma_accept')]
    review_needed = [r for r in results if r['decision'] not in ('auto_accept', 'gemma_accept')]
    print(f"\n--- summary: {len(accepted)} gate-passed, {len(review_needed)} need review ---")
    for r in review_needed:
        print(f"  NEEDS REVIEW: {r['slug']} ({r['decision']}, score {r['score']})")
    if not args.commit:
        print('\nDRY-RUN. Re-run with --commit to write gate-passing entries.')


if __name__ == '__main__':
    main()
