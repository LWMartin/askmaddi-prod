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
        'category': entry.get('category', ''),
        'contamination_key': entry.get('contamination_key', slug),
        'label': f"{vendor} {model}".strip(),
        'aliases': list(aliases),
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
def resolve_proposal(slug, *, ebay, gemma, demand_log, review_queue,
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

    Returns a structured outcome dict (never raises on a routing decision; only on
    a genuine precondition/API error):
      {'slug', 'outcome', 'detail', ...}
    where outcome is one of:
      'resolved'        — confident pick, written to spine. detail = upsert status.
      'queued'          — low-confidence pick, enqueued w/ candidates. detail=queue_id.
      'no_candidate'    — search/disambiguation found nothing; logged as unmet demand.

    Raises ResolveError if the slug has no registry entry. eBay API failures
    propagate as ebay_api.EbayAPIError (a transient problem the caller retries),
    NOT swallowed into a routing outcome — a network blip is not "no demand."
    """
    target = lookup_proposal(slug, skus_path=skus_path)

    # Build the search query from label + any aliases (aliases widen recall for
    # products whose market title differs from the controlled-vocab model string).
    query = target['label']
    candidates = ebay._search_candidates(query, limit=candidate_limit)
    # Drop rows with no item_id (can't resolve() them) before Gemma sees them.
    candidates = [c for c in candidates if c.get('item_id')]

    verdict = gemma.pick(target, candidates)

    # ── Route 1: nothing usable -> unmet demand (demand_log's exact purpose) ──
    if verdict['item_id'] is None:
        kwargs = {} if demand_log_path is None else {'path': demand_log_path}
        demand_log.log_unmet(target['category'], identity=None, **kwargs)
        return {
            'slug': slug, 'outcome': 'no_candidate',
            'detail': verdict['why'], 'confidence': verdict['confidence'],
            'candidates_seen': len(candidates),
        }

    # ── Route 2: low-confidence pick -> review with candidates visible ──
    if verdict['confidence'] < floor:
        resolved = ebay.resolve(verdict['item_id'])
        # A synthetic resolution carrying the proposal's frozen slug — the factory
        # already KNOWS the slug (it's a registry entry), so this enqueue is about
        # the eBay pick, not the slug. needs_review/collision are False; the reason
        # is forced to low_resolve_confidence.
        resolution = _FactoryResolution(slug=slug, input_text=target['label'])
        eq_kwargs = {} if review_queue_path is None else {'path': review_queue_path}
        record = review_queue.enqueue(
            resolution, resolved,
            target['vendor'], target['model'], target['category'],
            contamination_key=target['contamination_key'],
            reason_override='low_resolve_confidence',
            candidates=verdict['ranked'],
            **eq_kwargs,
        )
        return {
            'slug': slug, 'outcome': 'queued',
            'detail': record['queue_id'], 'confidence': verdict['confidence'],
            'why': verdict['why'], 'candidates_seen': len(candidates),
        }

    # ── Route 3: confident pick -> resolve + write spine (the existing chain) ──
    resolved = ebay.resolve(verdict['item_id'])
    entry = skus_registry.build_entry(
        slug=slug,
        vendor=target['vendor'],
        model=target['model'],
        category=target['category'],
        contamination_key=target['contamination_key'],
        resolved=resolved,
    )
    status = skus_registry.upsert(slug, entry, path=skus_path)
    return {
        'slug': slug, 'outcome': 'resolved',
        'detail': status, 'confidence': verdict['confidence'],
        'why': verdict['why'], 'item_id': verdict['item_id'],
        'candidates_seen': len(candidates),
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
