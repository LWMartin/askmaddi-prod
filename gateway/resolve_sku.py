"""
resolve_sku.py — demand-factory SKU resolver (proposal slug -> buildable identity).
====================================================================================
The factory's ONLY genuinely-new surface. Everything downstream of the item_id is
the existing, tested gate chain shared with the live /ebay/resolve route; this
module is strictly the front of it:

    proposal_slug (e.g. "sony-a7s-iii", fork=13)
      -> lookup_proposal()        # vendor/model/category/aliases from skus.json   [EXISTING registry]
      -> ebay_api._search_candidates(label/aliases)  # item_id-bearing rows         [EXISTING]
      -> GemmaDisambiguator.pick()                    # body vs accessory/bundle/gen [NEW — the only new judgment]
      -> route:
           no usable candidate        -> demand_log.log_unmet(category, None)        [EXISTING — its job]
           confidence >= floor         -> ebay_api.resolve(item_id)                   [EXISTING]
                                          -> skus_registry.build_entry/upsert         [EXISTING]
           confidence <  floor         -> review_queue.enqueue(reason_override=        [EXISTING + this session's
                                            'low_resolve_confidence', candidates=...)   candidates extension]

WHY THE RESOLVER IS THIN (the three seam-confirmations, 2026-06-29):
  Q1  ebay_api.resolve(item_id) DERIVES machine identity (epid, ebay_category_id,
      brand, mpn, image, price) from the eBay item itself — no caller input. The
      controlled-vocab vendor/model/category come from the EXISTING registry entry
      the proposal slug already points to, NOT manufactured by Gemma. So Gemma's
      only job is choosing the item_id; identity drift between factory-built and
      tap-built entries is structurally impossible.
  Q2  Proposal slugs are ALREADY registry entries (that's where the comparator got
      their aliases). The factory ENRICHES an existing entry with the eBay item_id
      + identity it's missing; it does not mint identity from scratch.
  Q3  slug_normalizer.resolve_slug resolves an already-present vendor/model to its
      FROZEN slug (needs_review=False) — the factory feeding existing slugs does
      NOT trip the slug gate. The only review path the factory creates is the
      RESOLVE-side one: a low-confidence eBay pick.

CONFIDENCE FLOOR = the resolver's "abstain -> human", mirroring the comparator
typer's abstain discipline. A pick below the floor is NOT silently resolved into a
possibly-wrong card (an accessory mistaken for the body); it lands in /admin with
its competing candidates visible, so a human catches the overlooked product. This
is the resolve-time analog of the 2026-06-17 near-miss sidecar's "catch a FALSE
DROP before deploy."

INJECTED CLIENTS (offline-testable, live-provable): ebay_api and the Gemma client
are box-only (creds + ollama). resolve_proposal takes injected `ebay` and `gemma`
so the routing logic is unit-tested offline with mocks; live resolution is proven
on the VPS. Same discipline as GemmaComparatorTyper(client=...).

This module performs NO filesystem writes of its own beyond the existing
primitives it calls (which write to box-side, gitignored data/ stores). It does
not touch the skus.json spine directly except through skus_registry.upsert (the
one sanctioned writer).
"""
import json
import re
import urllib.request
from pathlib import Path

import skus_registry
import slug_normalizer
import ebay_category_map
import rebind_firewall
import resolver_mfr_surface   # rung C (airlocked cell)
import resolver_xconfirm      # rung D (airlocked cell)

# Default eBay-candidate disambiguation model — the locked, validated checkpoint
# (gemma4:e2b-it-qat), same tag the comparator typer uses. Single-sourced default.
DEFAULT_MODEL = "gemma4:e2b-it-qat"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"

# Confidence at/above which a Gemma pick resolves straight through; below it the
# product lands in review with its candidates. 0.0..1.0. Tunable; the comparator
# work used a margin-above-coinflip — here the model emits an absolute confidence
# in its single chosen candidate, so the floor is a probability, not a margin.
DEFAULT_CONFIDENCE_FLOOR = 0.70

# How many eBay candidate rows to weigh. Enough to surface the real product even
# when accessories/bundles crowd the top, few enough to keep the Gemma prompt and
# the /admin candidates render compact.
DEFAULT_CANDIDATE_LIMIT = 8


class ResolveError(Exception):
    """A resolve_proposal() precondition failed (unknown slug, no registry entry).
    Distinct from ebay_api.EbayAPIError (a network/API failure) so the caller can
    tell a bad proposal from a transient eBay problem."""


# ──────────────────────────────────────────────────────────────────────────────
# Step 1 — proposal slug -> existing registry identity (Q2: enrich, don't mint)
# ──────────────────────────────────────────────────────────────────────────────
def lookup_proposal(slug, *, skus_path=skus_registry.SKUS_PATH):
    """Read the controlled-vocab identity for a proposal slug off the registry.

    The comparator proposals are SKUs already in skus.json (that's where the
    miner got their aliases), so this is a lookup, not a construction. Returns a
    dict {slug, vendor, model, category, contamination_key, label, aliases} where
    `label` is the eBay search seed (vendor + model) and `aliases` are extra
    search terms when the entry carries them.

    Raises ResolveError if the slug has no registry entry — the factory only
    resolves proposals that already have a controlled-vocab identity; a slug with
    no entry is a different problem (it should never have been proposed) and is
    surfaced loudly, not papered over.
    """
    registry = skus_registry.load_registry(skus_path)
    entry = (registry.get('skus') or {}).get(slug)
    if entry is None:
        raise ResolveError(
            f"proposal slug {slug!r} has no registry entry — the factory enriches "
            f"existing SKUs, it does not mint identity. A proposed slug with no "
            f"entry is an upstream bug (proposed something never registered)."
        )
    vendor = entry.get('vendor', '')
    model = entry.get('model', '')
    aliases = entry.get('aliases') or []
    return {
        'slug': slug,
        'vendor': vendor,
        'model': model,
        'category': skus_registry.get_facet(entry) or '',
        'contamination_key': entry.get('contamination_key', slug),
        'label': f"{vendor} {model}".strip(),
        'aliases': list(aliases),
        # Identity provenance for the resolver's routing. An existing entry is a
        # frozen registry fact -> 'resolved', never minted, never a collision.
        'source': 'resolved',
        'minted': False,
        'resolution': None,
    }


def lookup_or_mint(slug, *, vendor=None, model=None,
                   skus_path=skus_registry.SKUS_PATH):
    """Resolve a proposal's buildable identity, MINTING one if the slug is new.

    The minting wire's part (b/c). Supersedes the bare lookup_proposal() call in
    resolve_proposal: the demand miner proposes slugs from a vocab that is a
    SUPERSET of the registry, so a proposal slug frequently has no entry yet.
    Rather than the historical hard ResolveError, this:

      1. Tries the registry (lookup_proposal) — the ENRICH path, unchanged. An
         existing entry returns exactly as before, source='resolved', minted=False.

      2. On a registry miss, MINTS — but only if vendor+model travelled with the
         proposal (the identity shape). It calls slug_normalizer.resolve_slug(
         vendor, model), which is the ONE sanctioned slug minter:
           - frozen-by-identity: if this vendor/model secretly already has a
             registry entry under a hand-authored slug, resolve_slug returns that
             FROZEN slug (needs_review=False). We then re-enter the registry under
             the real slug — an enrich, not a mint. (Guards the sony-a7iv ~
             sony-a7-iv class.)
           - generated: a brand-new slug, needs_review=True, possibly with a
             collision flag (the Sigma sigma-35-f12-dg-dn vs minted form case).
         Category is NOT known yet on a mint (no registry row) — it is derived
         from eBay downstream (part e), so it returns '' here, filled in by the
         resolver after resolve().

      3. On a registry miss WITHOUT vendor/model (a legacy tuple-shape proposal,
         no identity): raises ResolveError, same as the historical behaviour —
         we cannot mint an identity we were never given, and a silent skip would
         lose the demand signal. Loud, not papered over.

    Returns the same dict shape as lookup_proposal plus:
      source     : 'resolved' (existing) | 'generated' (freshly minted)
      minted     : bool — True only for a genuinely new generated slug
      resolution : slug_normalizer.SlugResolution | None — carried so the
                   resolver can route a collision to review with the real
                   collision_with, and badge a generated mint.
    The returned 'slug' is the RESOLVED slug (may differ from the proposed one
    when resolve_slug froze it to a hand-authored form).
    """
    registry = skus_registry.load_registry(skus_path)
    entry = (registry.get('skus') or {}).get(slug)
    if entry is not None:
        # Existing -> the enrich path, byte-for-byte the old behaviour.
        return lookup_proposal(slug, skus_path=skus_path)

    # Registry miss. Mint only if identity travelled with the proposal.
    if not (vendor and model):
        raise ResolveError(
            f"proposal slug {slug!r} has no registry entry AND the proposal "
            f"carried no vendor/model to mint from. The factory can enrich an "
            f"existing SKU or mint a new one from identity, but a slug with "
            f"neither is unbuildable — emit the identity shape (vendor+model) "
            f"to enable minting, or register the slug."
        )

    res = slug_normalizer.resolve_slug(vendor, model, skus_path=Path(skus_path))

    if res.needs_review:
        # Genuinely new slug — a mint. Category derived from eBay later (part e),
        # so '' for now. contamination_key seeds to the minted slug (a starting
        # point a human can correct at review), matching review_queue's default.
        return {
            'slug': res.slug,
            'vendor': vendor,
            'model': model,
            'category': '',
            'contamination_key': res.slug,
            'label': f"{vendor} {model}".strip(),
            'aliases': [],
            'source': 'generated',
            'minted': True,
            'resolution': res,
        }

    # resolve_slug returned a FROZEN slug (override/identity/slug match) — the
    # vendor/model actually IS already registered, just under res.slug rather
    # than the proposed one. Re-enter the registry under the real slug: an
    # enrich, not a mint. (If res.slug also has no entry — a frozen-by-slug edge
    # where the slug exists in the override set but not as a full entry — fall
    # back to a mint-shaped identity so we never hard-fail a buildable product.)
    real = (registry.get('skus') or {}).get(res.slug)
    if real is not None:
        return lookup_proposal(res.slug, skus_path=skus_path)
    return {
        'slug': res.slug,
        'vendor': vendor,
        'model': model,
        'category': '',
        'contamination_key': res.slug,
        'label': f"{vendor} {model}".strip(),
        'aliases': [],
        'source': 'generated',
        'minted': True,
        'resolution': res,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Step 2 — Gemma disambiguation (the only new judgment)
# ──────────────────────────────────────────────────────────────────────────────
def _build_prompt(target, candidates):
    """Construct the disambiguation prompt: which candidate IS the target product.

    The judgment is PRAGMATIC (body vs accessory, bare vs bundle, this generation
    vs the predecessor) — exactly the class Gemma was validated on for comparator
    typing. We give it the controlled-vocab target and the numbered candidate
    titles, and ask for a single index + an absolute confidence + a one-line why.
    """
    lines = [
        "You are disambiguating eBay listings to identify ONE product.",
        "",
        f"TARGET PRODUCT (the exact item we want):",
        f"  vendor: {target['vendor']}",
        f"  model:  {target['model']}",
        f"  category: {target['category']}",
        "",
        "CANDIDATE LISTINGS (choose the ONE that is the bare target product",
        "itself — a camera BODY, not an accessory, kit lens, bundle, case, or a",
        "different generation):",
        "",
    ]
    for i, c in enumerate(candidates):
        price = f"{c.get('price','')} {c.get('currency','')}".strip()
        cond = c.get('condition', '')
        lines.append(f"  [{i}] {c.get('title','')}  ({price}; {cond})")
    lines += [
        "",
        "Respond with ONLY a JSON object, no prose, no markdown fences:",
        '  {"index": <int>, "confidence": <0.0-1.0>, "why": "<short reason>"}',
        "index is the [n] of the chosen candidate. confidence is how sure you are",
        "this candidate is the bare target product (not an accessory/bundle/wrong",
        "generation). If NONE of the candidates is the target, use index -1.",
    ]
    return "\n".join(lines)


def _parse_verdict(raw, n_candidates):
    """Parse the model's JSON verdict defensively.

    Tolerates a stray markdown fence or leading prose by extracting the first
    {...} block. Returns (index, confidence, why). index is clamped to
    [-1, n_candidates-1]; a malformed/empty response degrades to (-1, 0.0, ...)
    so the caller routes it to review rather than crashing — an unparseable
    verdict is itself low confidence.
    """
    if not raw:
        return -1, 0.0, "empty model response"
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return -1, 0.0, "no JSON object in response"
    try:
        obj = json.loads(m.group(0))
    except (ValueError, TypeError):
        return -1, 0.0, "unparseable JSON in response"
    try:
        idx = int(obj.get("index", -1))
    except (ValueError, TypeError):
        idx = -1
    try:
        conf = float(obj.get("confidence", 0.0))
    except (ValueError, TypeError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    if idx < -1 or idx >= n_candidates:
        idx = -1
    why = str(obj.get("why", ""))[:200]
    return idx, conf, why


class GemmaDisambiguator:
    """Choose which eBay candidate IS the target product, with a confidence.

    Mirrors comparator_gemma_typer.GemmaComparatorTyper's injectable-client shape:
    pass `client=<callable prompt->raw text>` for offline tests; leave it None to
    hit the live Ollama /api/generate on the box. The model tag defaults to the
    locked checkpoint so the disambiguator and the comparator typer share one
    validated model.

    pick(target, candidates) -> dict:
      {'index': int,           # chosen candidate index, or -1 for "none of these"
       'confidence': float,    # 0..1, absolute confidence in the bare-target pick
       'why': str,             # one-line model rationale
       'item_id': str|None,    # candidates[index]['item_id'], or None if index<0
       'ranked': list[dict]}   # candidates annotated with {score, chosen} for the
                               # /admin "was a valid product overlooked?" surface
    """

    def __init__(self, model=DEFAULT_MODEL, ollama_url=DEFAULT_OLLAMA_URL,
                 client=None):
        self.model = model
        self.ollama_url = ollama_url
        self.client = client  # inject a mock in tests; None -> live ollama

    def _generate(self, prompt):
        if self.client is not None:
            return self.client(prompt)
        payload = json.dumps({
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.0},
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.ollama_url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body.get("response", "")

    def pick(self, target, candidates):
        if not candidates:
            return {
                'index': -1, 'confidence': 0.0, 'why': 'no candidates',
                'item_id': None, 'ranked': [],
            }
        prompt = _build_prompt(target, candidates)
        raw = self._generate(prompt)
        idx, conf, why = _parse_verdict(raw, len(candidates))

        ranked = []
        for i, c in enumerate(candidates):
            row = dict(c)
            row['chosen'] = (i == idx)
            # The model emits one absolute confidence for its chosen pick; the
            # non-chosen rows carry score 0.0 (they're the "what else was on the
            # table" context for the human, not independently scored).
            row['score'] = conf if i == idx else 0.0
            ranked.append(row)

        item_id = candidates[idx]['item_id'] if 0 <= idx < len(candidates) else None
        return {
            'index': idx, 'confidence': conf, 'why': why,
            'item_id': item_id, 'ranked': ranked,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Step 3 — orchestration: lookup -> search -> pick -> route into the EXISTING chain
# ──────────────────────────────────────────────────────────────────────────────
def _norm_gtin(x):
    """Digits only, zero-padded to GTIN-14 so a feed GTIN-12/13 compares equal to
    the same product's canonical GTIN-14. Empty/no-digits -> '' (not comparable)."""
    d = re.sub(r'\D', '', str(x or ''))
    return d.zfill(14) if d else ''


def _norm_mpn(x):
    """Alphanumeric, upper — so 'ILCE-7SM3' == 'ilce7sm3'. Empty -> '' (skip)."""
    return re.sub(r'[^A-Z0-9]', '', str(x or '').upper())


def _id_agreement(target, identity):
    """Compare the proposal's carried GTIN/MPN against the resolved eBay item's
    identity (GTIN/MPN-first, spec step 2). GTIN is the stronger key and wins when
    both sides carry one; MPN is the fallback.

    Returns:
      'agree'      — an exact GTIN (or, absent GTINs, MPN) match: the pick IS the
                     proposed product. Strength beats Gemma's floor (rule 1).
      'contradict' — both sides carry the SAME id type but different values: the
                     carried id disproves the keyword+Gemma pick (rule 2, the
                     Kodak->nikon-z2 firewall). Never mint.
      'none'       — no shared id type to compare -> fall back to the existing
                     confidence routing (behaviour unchanged when the feed omits
                     an id or eBay didn't surface one).
    """
    identity = identity or {}
    t_gtin, i_gtin = _norm_gtin(target.get('gtin')), _norm_gtin(identity.get('gtin'))
    if t_gtin and i_gtin:
        return 'agree' if t_gtin == i_gtin else 'contradict'
    t_mpn, i_mpn = _norm_mpn(target.get('mpn')), _norm_mpn(identity.get('mpn'))
    if t_mpn and i_mpn:
        return 'agree' if t_mpn == i_mpn else 'contradict'
    return 'none'


def resolve_proposal(slug, *, ebay, gemma, demand_log, review_queue,
                     vendor=None, model=None,
                     gtin=None, mpn=None, product_url=None,
                     log_unmet_on_miss=True,
                     floor=DEFAULT_CONFIDENCE_FLOOR,
                     candidate_limit=DEFAULT_CANDIDATE_LIMIT,
                     skus_path=skus_registry.SKUS_PATH,
                     review_queue_path=None, demand_log_path=None):
    """Resolve one comparator proposal slug into the demand-factory pipeline.

    Pure orchestration over INJECTED collaborators so the routing is unit-tested
    offline:
      ebay         — module/obj exposing ._search_candidates(query, limit) and
                     .resolve(item_id)  (production: the ebay_api module)
      gemma        — a GemmaDisambiguator (production: client=None -> live ollama)
      demand_log   — the demand_log module (.log_unmet)
      review_queue — the review_queue module (.enqueue)
      vendor, model — OPTIONAL identity carried by the proposal (the minting-wire
                     identity shape). When present, a slug that is NOT yet a
                     registry entry is MINTED rather than erroring; when absent,
                     the historical enrich-only behaviour holds (ResolveError on
                     a missing slug).

    Returns a structured outcome dict (never raises on a routing decision; only on
    a genuine precondition/API error):
      {'slug', 'outcome', 'detail', ...}
    where outcome is one of:
      'resolved'        — confident pick, written to spine. detail = upsert status.
                          May be a freshly MINTED entry (source='generated',
                          minted_needs_review=True) or an enriched existing one.
      'queued'          — enqueued for review. detail=queue_id. Two reasons:
                          'collision' (a minted slug clashes with an existing
                          spine slug — reconcile before building) or
                          'low_resolve_confidence' (uncertain eBay pick).
      'no_candidate'    — search/disambiguation found nothing; logged as unmet demand.

    Raises ResolveError only when the slug has no registry entry AND no identity
    to mint from. eBay API failures propagate as ebay_api.EbayAPIError (a
    transient problem the caller retries), NOT swallowed into a routing outcome.
    """
    target = lookup_or_mint(slug, vendor=vendor, model=model, skus_path=skus_path)
    # Carry the proposal's feed identity onto the target (GTIN/MPN-first, step 2).
    # resolve_multisource reads these to gate the eBay pick on id agreement; the
    # single-source path below ignores them, so behaviour is unchanged until the
    # ladder is wired.
    for _k, _v in (('gtin', gtin), ('mpn', mpn), ('product_url', product_url)):
        if _v and not target.get(_k):
            target[_k] = _v
    # The resolved slug may differ from the proposed one (resolve_slug can freeze
    # a hand-authored form). Use it consistently from here on.
    slug = target['slug']

    # ── Route 0 (mint only): slug COLLISION -> review before building ──────────
    # A minted slug that normalizes the same as an existing spine slug (the Sigma
    # sigma-35-f12-dg-dn ~ sigma-35mm-f-1-2-dg-dn-art class) is a silent
    # duplicate-under-different-punctuation hazard the publish eyeball won't
    # reliably catch — it corrupts the registry join. Unlike a clean mint (which
    # the publish air-gap reviews), a collision must be reconciled at the SLUG
    # level by a human, so it never reaches the spine. Routed before eBay is even
    # touched: there's nothing to build until the slug question is settled.
    resolution = target.get('resolution')
    if target.get('minted') and resolution is not None and resolution.collision:
        eq_kwargs = {} if review_queue_path is None else {'path': review_queue_path}
        # No eBay identity yet (we stopped before search), so enqueue with an
        # empty resolved block; review_queue freezes what identity it's given and
        # the human resolves the slug clash. reason derives from the resolution
        # (collision is set) -> 'collision', the badge /admin already renders.
        record = review_queue.enqueue(
            resolution, {}, target['vendor'], target['model'], target['category'],
            contamination_key=target['contamination_key'],
            **eq_kwargs,
        )
        return {
            'slug': slug, 'outcome': 'queued', 'detail': record['queue_id'],
            'reason': 'collision', 'collision_with': resolution.collision,
            'source': target['source'],
        }

    # Build the search query from label + any aliases (aliases widen recall for
    # products whose market title differs from the controlled-vocab model string).
    query = target['label']
    candidates = ebay._search_candidates(query, limit=candidate_limit)
    # Drop rows with no item_id (can't resolve() them) before Gemma sees them.
    candidates = [c for c in candidates if c.get('item_id')]

    verdict = gemma.pick(target, candidates)

    # ── Route 1: nothing usable -> unmet demand (demand_log's exact purpose) ──
    # When an escalation ladder wraps this call (resolve_multisource), it defers
    # the unmet log to the TRUE floor — after the mfr/Icecat rungs also miss — so
    # log_unmet stays a truthful "no identity anywhere", not "not on eBay".
    if verdict['item_id'] is None:
        if log_unmet_on_miss:
            kwargs = {} if demand_log_path is None else {'path': demand_log_path}
            demand_log.log_unmet(target['category'], identity=None, **kwargs)
        return {
            'slug': slug, 'outcome': 'no_candidate',
            'detail': verdict['why'], 'confidence': verdict['confidence'],
            'candidates_seen': len(candidates), 'source': target['source'],
            'category': target['category'],
        }

    # Resolve the chosen item ONCE — its identity is needed BOTH to verify the
    # carried id AND to write the spine (no extra eBay round-trip vs before).
    resolved = ebay.resolve(verdict['item_id'])

    # ── Identity gate (GTIN/MPN-first, spec step 2) ──────────────────────────
    # The carried feed id is stronger than a keyword+Gemma title pick. CONTRADICT
    # -> the pick is wrong-product; route to review, never mint (the
    # Kodak->nikon-z2 firewall). AGREE -> the pick is certain; straight-through
    # even below Gemma's floor (strength beats the floor). No shared id -> the
    # historical confidence routing is unchanged.
    agreement = _id_agreement(target, resolved.get('identity'))
    if agreement == 'contradict':
        resolution = _FactoryResolution(slug=slug, input_text=target['label'])
        eq_kwargs = {} if review_queue_path is None else {'path': review_queue_path}
        category = target['category'] or ebay_category_map.category_for(
            resolved.get('identity', {}).get('ebay_category_id', ''))
        record = review_queue.enqueue(
            resolution, resolved,
            target['vendor'], target['model'], category,
            contamination_key=target['contamination_key'],
            reason_override='identity_contradiction',
            candidates=verdict['ranked'],
            **eq_kwargs,
        )
        return {
            'slug': slug, 'outcome': 'queued',
            'detail': record['queue_id'], 'reason': 'identity_contradiction',
            'confidence': verdict['confidence'], 'why': verdict['why'],
            'candidates_seen': len(candidates), 'source': target['source'],
        }
    deterministic_id = (agreement == 'agree')

    # ── Route 2: low-confidence pick -> review (UNLESS a deterministic id match
    # vouches for it — rule 1) ──
    if verdict['confidence'] < floor and not deterministic_id:
        # A synthetic resolution carrying the (resolved) slug — the enqueue is
        # about the eBay PICK, not the slug, so the reason is forced to
        # low_resolve_confidence regardless of whether the slug was minted.
        resolution = _FactoryResolution(slug=slug, input_text=target['label'])
        eq_kwargs = {} if review_queue_path is None else {'path': review_queue_path}
        # On the mint path category is still '' (eBay-derived); fill it from the
        # resolved item so the review record carries a best-effort category.
        category = target['category'] or ebay_category_map.category_for(
            resolved.get('identity', {}).get('ebay_category_id', ''))
        record = review_queue.enqueue(
            resolution, resolved,
            target['vendor'], target['model'], category,
            contamination_key=target['contamination_key'],
            reason_override='low_resolve_confidence',
            candidates=verdict['ranked'],
            **eq_kwargs,
        )
        return {
            'slug': slug, 'outcome': 'queued',
            'detail': record['queue_id'], 'confidence': verdict['confidence'],
            'why': verdict['why'], 'candidates_seen': len(candidates),
            'source': target['source'],
        }

    # ── Route 3: confident (or deterministic-id) pick -> write spine ──

    # Category: an existing entry already has its controlled-vocab category; a
    # MINTED entry has none yet, so derive it from the eBay item (part e, the
    # "marketplace truth, no hand-tagging" decision). An unknown category id maps
    # to '' -> the mint stays needs_review so Lee fills it at the publish gate.
    category = target['category']
    minted = target.get('minted', False)
    if not category:
        category = ebay_category_map.category_for(
            resolved.get('identity', {}).get('ebay_category_id', ''))
    # A mint needs review at publish if it was generated OR its category came
    # back unknown (blank) — either is a "look harder" signal for the air gap.
    minted_needs_review = bool(minted) and (
        target.get('source') == 'generated' or not category)

    entry = skus_registry.build_entry(
        slug=slug,
        vendor=target['vendor'],
        model=target['model'],
        facet=category,
        contamination_key=target['contamination_key'],
        resolved=resolved,
        source=target.get('source', 'resolved'),
        minted_needs_review=minted_needs_review,
    )
    # ── Rebind firewall (found live 2026-07-16: empty-box rebind) ──
    # A REBIND — fresh identity differing from a standing spine entry — gets
    # a sanity gate the disambiguator's confidence verdict does not provide:
    # confidence judged WHICH candidate wins; this judges whether the winner
    # is plausibly still the same product. On reject the standing identity
    # stays, the suspect identity parks as an /admin receipt, and the pass
    # returns loud. First resolves and idempotent re-resolves never enter.
    standing = skus_registry.load_registry(skus_path).get('skus', {}).get(slug)
    if standing is not None and standing.get('identity') and \
            not skus_registry._same_identity(standing, entry):
        fw = rebind_firewall.assess(standing, entry)
        if fw['verdict'] == 'reject':
            receipt = {
                'ts': __import__('time').strftime('%Y-%m-%dT%H:%M:%SZ', __import__('time').gmtime()),
                'item_id': entry.get('marketplace_ids', {}).get('ebay_legacy_item_id', ''),
                'title': entry.get('identity', {}).get('market_title', ''),
                'price_seen': entry.get('identity', {}).get('price_seen'),
                'signals': fw['signals'], 'hard': fw['hard'],
            }
            skus_registry.set_rebind_rejection(slug, receipt, path=skus_path)
            return {
                'slug': slug, 'outcome': 'rebind_rejected',
                'detail': ','.join(fw['signals']),
                'confidence': verdict['confidence'], 'why': verdict['why'],
                'item_id': verdict['item_id'],
                'candidates_seen': len(candidates),
                'source': target.get('source', 'resolved'),
            }

    status = skus_registry.upsert(slug, entry, path=skus_path)
    return {
        'slug': slug, 'outcome': 'resolved',
        'detail': status, 'confidence': verdict['confidence'],
        'why': verdict['why'], 'item_id': verdict['item_id'],
        'candidates_seen': len(candidates),
        'source': target.get('source', 'resolved'),
        'minted': minted, 'category': category,
        'minted_needs_review': minted_needs_review,
        'deterministic': deterministic_id,
    }


class _FactoryResolution:
    """Minimal slug_normalizer.SlugResolution-shaped object for the factory's
    low-confidence enqueue path.

    review_queue.enqueue reads .slug, .collision, .input_text off the resolution.
    The factory's slug is already a frozen registry fact (resolve_slug would return
    it clean), so collision is None and needs_review is irrelevant — the record's
    reason is forced via reason_override, not derived from this object. We pass a
    tiny shim rather than calling slug_normalizer.resolve_slug again because the
    slug is known and re-resolving would be a redundant registry read.
    """
    __slots__ = ('slug', 'collision', 'input_text', 'needs_review', 'source')

    def __init__(self, slug, input_text):
        self.slug = slug
        self.collision = None
        self.input_text = input_text
        self.needs_review = False
        self.source = 'override'


def _best_sourced(c_res, d_res, floor):
    """The strongest CONFIDENT off-market resolution carrying a real identity, or
    None. Rung D (cross-confirmed, holds relations) is preferred over C at equal
    strength; deterministic or confidence>=floor qualifies. Ambiguous is excluded
    (handled separately, before this)."""
    for r in (d_res, c_res):
        if (r and r.get('identity') and not r.get('ambiguous')
                and (r.get('deterministic') or r.get('confidence', 0.0) >= floor)):
            return r
    return None


def _enqueue_sourced(slug, review_queue, review_queue_path, vendor, model,
                     c_res, d_res, *, reason, why):
    """Propose an OFF-MARKET identity (rung C/D, no eBay listing) onto the /admin
    review surface. The matcher proposes; the human promotes (enrich-don't-mint +
    the publish air gap hold). Carries the pooled aliases + relations as the
    contamination material the promote step needs."""
    best = d_res if (d_res and d_res.get('identity')) else c_res
    ident = (best or {}).get('identity') or {}
    relations = ((d_res or {}).get('relations') or (c_res or {}).get('relations')
                 or {'predecessor': [], 'competitor': []})
    aliases = []
    for r in (c_res, d_res):
        for a in ((r or {}).get('aliases') or []):
            if a not in aliases:
                aliases.append(a)
    # Shape a resolved-like block review_queue can freeze (it reads .identity and
    # .affiliate_url defensively — no eBay-specific keys required).
    resolved = {
        'identity': {
            'gtin': ident.get('gtin'), 'mpn': ident.get('mpn'),
            'brand': ident.get('brand') or vendor,
            'market_title': ident.get('canonical_model') or model,
            'image': ident.get('image'),
        },
        'affiliate_url': '',
    }
    resolution = _FactoryResolution(slug=slug,
                                    input_text=f"{vendor or ''} {model or ''}".strip())
    eq_kwargs = {} if review_queue_path is None else {'path': review_queue_path}
    record = review_queue.enqueue(
        resolution, resolved, vendor, model, '',  # category unknown off-market
        contamination_key=slug,
        reason_override=reason,
        contamination={'source': (best or {}).get('source'),
                       'aliases': aliases, 'relations': relations, 'why': why},
        **eq_kwargs,
    )
    return {
        'slug': slug, 'outcome': 'queued', 'detail': record['queue_id'],
        'reason': reason, 'why': why, 'source': (best or {}).get('source'),
        'sourced': True,
    }


def resolve_multisource(slug, *, ebay, gemma, demand_log, review_queue,
                        mfr_surface=resolver_mfr_surface,
                        xconfirm=resolver_xconfirm,
                        vendor=None, model=None,
                        gtin=None, mpn=None, product_url=None,
                        floor=DEFAULT_CONFIDENCE_FLOOR,
                        candidate_limit=DEFAULT_CANDIDATE_LIMIT,
                        skus_path=skus_registry.SKUS_PATH,
                        review_queue_path=None, demand_log_path=None):
    """The GTIN/MPN-first identity ladder (spec maddi-multisource-identity-matcher).

    Wraps resolve_proposal (rung A registry + rung B eBay+Gemma, now id-gated) and
    adds the escalation the single-source path lacked: when eBay has no usable
    candidate, fall to the manufacturer/Shopify surface (rung C) then the
    Icecat/Wikidata cross-confirm (rung D) instead of dead-ending at log_unmet.

    Gate rules (the cerebral core):
      1. Deterministic short-circuit  — an exact GTIN/MPN match straight-throughs
         (inside resolve_proposal's id gate).
      2. Contradiction -> human       — a carried id disproving the pick routes to
         review (inside resolve_proposal; the Kodak->nikon-z2 firewall).
      3. Ambiguity -> cross-confirm   — >1 real product across eras (rung D
         ambiguous, the panasonic-lumix-l10 class) routes to review, never guesses.

    A confident OFF-MARKET identity (rung C/D, no eBay listing) is PROPOSED to
    /admin (reason 'sourced_offmarket'), never written to the spine directly.
    log_unmet fires only after EVERY rung misses — the true "no identity anywhere"
    floor. mfr_surface / xconfirm are the injected airlocked cells; pass a stub
    whose .resolve returns None to disable a rung in a test.
    """
    outcome = resolve_proposal(
        slug, ebay=ebay, gemma=gemma, demand_log=demand_log,
        review_queue=review_queue, vendor=vendor, model=model,
        gtin=gtin, mpn=mpn, product_url=product_url,
        log_unmet_on_miss=False,   # defer to the true floor below
        floor=floor, candidate_limit=candidate_limit, skus_path=skus_path,
        review_queue_path=review_queue_path, demand_log_path=demand_log_path)

    # eBay reached a decision (resolved / queued / rebind_rejected) — nothing to
    # escalate. Only a genuine eBay MISS falls through to the source rungs.
    if outcome.get('outcome') != 'no_candidate':
        return outcome

    src_target = {'vendor': vendor, 'model': model, 'gtin': gtin, 'mpn': mpn,
                  'source_url': product_url,
                  'brand': vendor, 'canonical_model': model}
    c_res = mfr_surface.resolve(src_target) if mfr_surface else None
    hint = (c_res or {}).get('identity') or {}
    d_res = xconfirm.resolve(src_target, hint) if xconfirm else None

    # Rule 3 — ambiguity across eras -> human, never guess a spine.
    if d_res and d_res.get('ambiguous'):
        return _enqueue_sourced(slug, review_queue, review_queue_path,
                                vendor, model, c_res, d_res,
                                reason='ambiguous_identity',
                                why=d_res.get('why', 'ambiguous across eras'))

    sourced = _best_sourced(c_res, d_res, floor)
    if sourced is not None:
        return _enqueue_sourced(slug, review_queue, review_queue_path,
                                vendor, model, c_res, d_res,
                                reason='sourced_offmarket',
                                why=sourced.get('why', 'off-market identity'))

    # Floor — every rung missed: the truthful unmet-demand signal.
    kwargs = {} if demand_log_path is None else {'path': demand_log_path}
    demand_log.log_unmet(outcome.get('category') or '', identity=None, **kwargs)
    outcome['escalated'] = True
    return outcome
