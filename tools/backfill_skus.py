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
import os
import re
import sys
from pathlib import Path

# gateway/ is the home of ebay_api + skus_registry; add it to the path.
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / 'gateway'))


def _load_gateway_env():
    """Source gateway/.env into os.environ BEFORE importing ebay_api.

    ebay_api reads creds from os.environ at *import* time, and the gateway's
    own .env loader only runs when app_production starts — which this tool does
    not import. So without this, the back-fill sees EBAY_* unset even with the
    .env file present on the box. Mirrors app_production._load_dotenv: no
    external dep, only sets keys not already in the environment (so systemd
    EnvironmentFile / shell exports still win). Silent no-op if absent.
    """
    env_path = ROOT / 'gateway' / '.env'
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception as e:
        print(f'[backfill] WARN: .env load failed: {type(e).__name__}')


_load_gateway_env()           # MUST precede ebay_api import (module-level cred reads)

import ebay_api          # noqa: E402
import skus_registry     # noqa: E402
import slug_normalizer   # noqa: E402  (graduated into gateway/, 2026-06-24)

CARDS_DIR = ROOT / 'data' / 'cards'

# Confidence gate: min token-overlap score (0..1) to auto-accept a candidate.
# Below this -> needs_review (or Gemma escalation if --gemma).
ACCEPT_THRESHOLD = 0.60

# A clear single winner must beat the runner-up by at least this margin to
# auto-accept. When several survivors cluster within the margin (range cards,
# material splits, same-generation siblings), the scorer refuses to guess and
# escalates to Gemma / review — the 2026-06-23 lesson.
DOMINANCE_MARGIN = 0.15

# contamination_key bridge: prod slug -> hyphenated editorial key. The two
# differ in form by design (spec decision #6); this is the explicit mapping,
# not a generated transform. Seed cadre only; extend as cards are added.
CONTAMINATION_KEY = {
    'sony-a7iv': 'sony-a7-iv',
    'sigma-35-art-dg-dn-ii': 'sigma-35-art-dg-dn-ii',
    'peak-design-travel-tripod': 'peak-design-travel-tripod',
    'peak-design-pro-tripod': 'peak-design-pro-tripod',
}

# Category back-fill for cards missing it (flagged, not silently authoritative).
# sigma lens card carries category=None; lens is the correct controlled value.
CATEGORY_DEFAULT = {'sigma-35-art-dg-dn-ii': 'lens'}

_TOKEN_RE = re.compile(r'[a-z0-9]+')


def _tokens(text):
    return set(_TOKEN_RE.findall((text or '').lower()))


# Generation/ordinal marks: if the card carries one, a candidate lacking it is
# a different generation and is disqualified by the coarse gate (Sigma Art II
# vs Art I). Roman/ordinal only — category-general, no part-number guessing
# (which falsely tripped on '35mm','a7iv','33mp' — that discrimination is now
# Gemma's job, not the scorer's).
_GENERATION_TOKENS = {'ii', 'iii', 'iv', 'v', 'mark', 'mk', 'm2', 'm3', 'm4'}

# Noise tokens that match everything — never identity-bearing.
_NOISE_TOKENS = {'the', 'a', 'an', 'for', 'with', 'and', 'mm', 'f',
                 'new', 'in', 'box', 'sale', 'free', 'shipping', 'tag', 'tags',
                 'lens', 'camera', 'mirrorless', 'body', 'black'}


# Markers that a listing is a multi-item BUNDLE rather than the clean single
# product. Down-weighted so they don't AUTO-win (they often spell out the MPN
# and over-score) — they escalate to Gemma, whose rules prefer a clean
# canonical listing. NOT a precise used-vs-new classifier (tokens can't do that
# reliably — that's Gemma's job); just enough to stop a bundle auto-accepting.
# Deliberately EXCLUDES common legit-listing words (shutter/count/read/fair):
# those appear on clean body listings too and would muddy the ranking Gemma
# sees. Targets genuine multi-item signals only.
_USED_BUNDLE_TOKENS = {'bundle', 'lenses', 'kit', 'combo', 'lot'}
_USED_BUNDLE_PENALTY = 0.6   # multiplier; enough to drop below DOMINANCE_MARGIN


def _ref_tokens(card_idn):
    """The card's identity token set: brand + model + alt-names, noise removed."""
    ref = set()
    ref |= _tokens(card_idn.get('brand'))
    ref |= _tokens(card_idn.get('model'))
    for alt in (card_idn.get('sku_alt_names') or []):
        ref |= _tokens(alt)
    return ref - _NOISE_TOKENS


def _score(card_idn, candidate_title):
    """COARSE identity score (0..1): generation hard-gate + overlap ranking.

    Deliberately does ONE robust, category-general hard filter and otherwise
    just ranks — it does NOT try to make the fine same-model/variant call that
    token patterns are bad at (the 2026-06-23 lesson: '35mm', 'a7iv', '33mp'
    all falsely tripped an MPN regex). Fine discrimination is Gemma's job.

      - HARD GATE: any generation token the card carries (e.g. 'ii' for the
        Sigma Art II) MUST appear in the candidate, else 0.0. Kills the
        Art-vs-Art-II / Mark-II class of miss. Roman/ordinal marks only —
        category-general, no part-number guessing.
      - RANK: recall-weighted token overlap, then a used/bundle DOWN-weight so
        a noisy used-copy listing does not auto-win over a clean canonical one
        (it escalates to Gemma instead). The top score is NOT a confident single
        winner when survivors are close — the caller escalates.
    """
    ref = _ref_tokens(card_idn)
    if not ref:
        return 0.0
    cand = _tokens(candidate_title)

    required = ref & _GENERATION_TOKENS
    if required and not required.issubset(cand):
        return 0.0

    hit = ref & cand
    score = len(hit) / len(ref)

    # Down-weight used/bundle listings so they escalate rather than auto-accept.
    if cand & _USED_BUNDLE_TOKENS:
        score *= _USED_BUNDLE_PENALTY

    return score


def _build_gemma_prompt(card_idn, candidates):
    """Text prompt for the orient shim: pick the representative candidate by NUMBER.

    The shim is text-in/text-out ({prompt}->{text}); it does not accept
    structured JSON. So we number the candidates and ask Gemma to reply with the
    number of the representative (or 0 for none). Mirrors the proven
    gemma_orienter prompt style: explicit rules, single-token-ish answer.
    """
    alts = ', '.join(card_idn.get('sku_alt_names') or []) or '(none)'
    lines = [
        'You are matching an eBay listing to a known product.',
        '',
        f"PRODUCT: {card_idn.get('brand', '')} {card_idn.get('model', '')}".strip(),
        f"  also known as: {alts}",
        '',
        'CANDIDATE LISTINGS:',
    ]
    for i, c in enumerate(candidates, 1):
        lines.append(f"  {i}. {c['title']}")
    lines += [
        '',
        'Reply with ONLY the number of the listing that is the canonical',
        'REPRESENTATIVE of this exact product, or 0 if none qualifies. Rules:',
        '1. Same generation and model ONLY. Reject siblings (A7 IV vs A7R IV;',
        '   DG DN Art vs DG DN Art II).',
        '2. For a RANGE product (several variants like Lite/Tall, or a material',
        '   split like aluminum/carbon), choose the BASE/STANDARD member,',
        '   preferring carbon over aluminum and the plain variant over Tall/Lite.',
        '3. Prefer a clean canonical listing over a used bundle when both are the',
        '   right model.',
        '',
        'Answer (a single number):',
    ]
    return '\n'.join(lines)


def _gemma_adjudicate(card_idn, candidates):
    """Adjudicate close survivors via the Gemma orient shim (127.0.0.1:5101).

    Off unless --gemma. Returns the chosen item_id or None. Speaks the REAL
    shim contract: POST {prompt, max_tokens, temperature} -> {text}. We send a
    numbered-choice prompt and parse the integer Gemma replies; map it back to
    the candidate's item_id. A hook, not a hard dependency: shim down / parse
    fail -> None, and the card falls to review rather than erroring the run.
    """
    import re as _re
    try:
        import requests
        prompt = _build_gemma_prompt(card_idn, candidates)
        r = requests.post('http://127.0.0.1:5101/orient',
                          json={'prompt': prompt, 'max_tokens': 8, 'temperature': 0.0},
                          timeout=60)
        if r.status_code != 200:
            print(f'  [gemma] shim HTTP {r.status_code}; falling to review')
            return None
        text = (r.json() or {}).get('text', '')
        m = _re.search(r'\d+', text)
        if not m:
            print(f'  [gemma] unparseable reply {text!r}; falling to review')
            return None
        pick = int(m.group(0))
        if pick < 1 or pick > len(candidates):
            return None          # 0 = none qualifies, or out of range
        return candidates[pick - 1]['item_id']
    except Exception as e:
        print(f'  [gemma] escalation unavailable ({e}); falling to review')
    return None


class SlugRejected(Exception):
    """A card's frozen slug failed the registry slug gate — HARD reject.

    Raised before build_entry when slug_normalizer says the card's authored
    slug is ambiguous (an unreviewed generated proposal) or collides with an
    existing-but-different spine slug under normalization. We do NOT write a
    flagged/quarantined entry into skus.json (the spine stays pure — the
    2026-06-23 Sigma lesson: a wrong value in canonical data defended only by
    readers remembering to check a flag is the silent-failure class the
    join-check exists to kill). Instead we stop, surface the proposal + the
    collision, and let Lee decide and re-run with an explicit --override.
    """


def _gate_slug(slug, vendor, model, *, override=None):
    """Verify the card's frozen filename-slug agrees with the registry's view,
    and is collision-free, BEFORE it is frozen into skus.json.

    The filename slug is itself a hand-authored fact (the card file was named
    deliberately). So this is not "what should the slug be" — it is "does the
    authored slug agree with what the override table resolves, and does it clash
    with any existing spine slug under normalization." For the seed cadre this
    is a no-op: identity-lookup resolves vendor/model to the frozen slug, which
    equals the filename, no collision. For a genuinely new card it is the gate.

    Resolution passes the filename slug as the explicit override so the SAME
    slug the writer will freeze is the one checked for collisions against the
    rest of the spine. Returns the resolution on PASS; raises SlugRejected on a
    collision (or, when the authored slug is absent, on an unreviewed generated
    proposal). An operator-supplied --override is honored verbatim but STILL
    collision-checked — a human override that clashes is still a hard reject.

    Reachability note: from backfill_card the `needs_review` reject is NOT
    reachable — backfill always supplies the card's filename as the authored
    slug, and an override always resolves needs_review=False, so COLLISION is
    backfill's live reject path. The needs_review branch is for the FUTURE route
    writer, which has no filename to lean on and will call this with no authored
    slug; there a generated proposal must hard-reject (→ 409 + review queue)
    rather than silently freeze. Keeping the branch here means both callers share
    one gate, per decision #3 ("the same function from the request handler").
    """
    chosen_override = override or slug
    res = slug_normalizer.resolve_slug(vendor, model, override=chosen_override)

    if res.collision:
        raise SlugRejected(
            f"slug '{res.slug}' normalizes the same as existing spine slug "
            f"'{res.collision}' (sony-a7iv ~ sony-a7-iv class). Refusing to "
            f"freeze a colliding slug into the spine. Resolve the clash, then "
            f"re-run with an explicit --override."
        )
    if res.needs_review:
        # Only reachable if the authored slug did not resolve to a frozen fact
        # AND no override was given — i.e. a generated proposal. The slug must
        # be human-confirmed before it enters the spine.
        raise SlugRejected(
            f"slug for {vendor!r} {model!r} is an UNREVIEWED generated proposal "
            f"({res.line()}). The spine only accretes slugs that were never in "
            f"doubt. Confirm the slug and re-run with --override <slug>."
        )
    return res


def backfill_card(slug, card, use_gemma=False, limit=10, override=None):
    """Resolve one card -> proposed skus entry + a review record. No writes.

    Two-stage selection (coarse scorer + semantic adjudicator):
      1. _score() coarse-filters (generation hard-gate) and ranks survivors.
      2. If ONE survivor clearly dominates (top >= ACCEPT_THRESHOLD and well
         clear of #2), auto-accept it.
      3. If survivors are CLOSE (the range / material-split / same-generation-
         sibling ambiguity — Pro vs Pro Tall, AL vs CF, A7 IV vs A7R IV), the
         scorer must NOT guess. Escalate to Gemma when --gemma, else mark
         needs_review. Gemma picks the representative (base/standard variant,
         carbon-preferred for range cards) or returns none.
    """
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
    survivors = [(s, c) for s, c in scored if s > 0]   # passed the generation gate
    review['rejected'] = [
        {'title': c['title'][:80], 'score': round(s, 3)} for s, c in scored[1:5]
    ]
    if not survivors:
        review['decision'] = 'no_survivors'   # everything failed the coarse gate
        review['score'] = 0.0
        return None, review

    top_score, top = survivors[0]
    runner = survivors[1][0] if len(survivors) > 1 else 0.0
    review['score'] = round(top_score, 3)

    # Clear single winner: dominant AND meaningfully ahead of #2.
    clear_winner = (top_score >= ACCEPT_THRESHOLD
                    and (len(survivors) == 1 or top_score - runner >= DOMINANCE_MARGIN))

    chosen = None
    if clear_winner:
        chosen = top
        review['decision'] = 'auto_accept'
    elif use_gemma:
        gid = _gemma_adjudicate(idn, [c for _, c in survivors[:5]])
        review['gemma_used'] = True
        if gid:
            chosen = next((c for _, c in survivors if c['item_id'] == gid), None)
            review['decision'] = 'gemma_accept' if chosen else 'gemma_no_match'
        else:
            review['decision'] = 'gemma_no_match'
    else:
        review['decision'] = 'needs_review'   # close survivors, no Gemma -> human

    if chosen is None:
        review['chosen'] = {'title': top['title'][:80], 'item_id': top['item_id']}
        return None, review

    review['chosen'] = {'title': chosen['title'][:80], 'item_id': chosen['item_id']}

    # Resolve the chosen item for lossless identity, customid = the frozen slug.
    resolved = ebay_api.resolve(chosen['item_id'], customid=slug)
    category = card.get('category') or CATEGORY_DEFAULT.get(slug)
    if card.get('category') is None and slug in CATEGORY_DEFAULT:
        review['category_backfilled'] = category

    vendor = idn.get('brand') or idn.get('vendor') or ''
    model = idn.get('model') or ''
    # GATE: the authored slug must agree with the registry's view and be
    # collision-free before it is frozen. No-op for the seed cadre (identity
    # resolves to the frozen slug); a hard wall for a new/ambiguous/colliding
    # slug. Raises SlugRejected (caught in main) rather than writing a flagged
    # entry — the spine never accretes a slug that was in doubt.
    res = _gate_slug(slug, vendor, model, override=override)
    review['slug_gate'] = res.source   # 'override' on pass

    entry = skus_registry.build_entry(
        slug=slug,
        vendor=vendor,
        model=model,
        category=category,
        contamination_key=CONTAMINATION_KEY.get(slug, slug),
        resolved=resolved,
    )
    return entry, review


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--card', help='back-fill a single slug (default: all)')
    ap.add_argument('--override',
                    help='explicit authored slug for the --card being seeded; '
                         'honored verbatim but still collision-checked. Use to '
                         'confirm a new card whose generated slug was rejected.')
    ap.add_argument('--commit', action='store_true',
                    help='write gate-passing entries to skus.json (default: dry-run)')
    ap.add_argument('--gemma', action='store_true',
                    help='escalate ambiguous picks to the Gemma shim (:5101)')
    ap.add_argument('--limit', type=int, default=10)
    args = ap.parse_args()

    slugs = ([args.card] if args.card
             else sorted(p.stem for p in CARDS_DIR.glob('*.json')))

    if args.override and not args.card:
        ap.error('--override only applies to a single --card; it is a per-card '
                 'slug confirmation, not a run-wide setting.')

    results = []
    for slug in slugs:
        card_path = CARDS_DIR / f'{slug}.json'
        if not card_path.exists():
            print(f'[skip] {slug}: no card file')
            continue
        card = json.loads(card_path.read_text(encoding='utf-8'))
        print(f'\n=== {slug} ===')
        try:
            entry, review = backfill_card(slug, card, use_gemma=args.gemma,
                                          limit=args.limit, override=args.override)
        except SlugRejected as e:
            # HARD reject — the slug was ambiguous or colliding. Nothing written;
            # the spine stays pure. Surface the reason and how to resolve it.
            print(f'  SLUG REJECTED: {e}')
            results.append({'slug': slug, 'decision': 'slug_rejected', 'score': 0.0,
                            'chosen': None, 'rejected': []})
            continue
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
