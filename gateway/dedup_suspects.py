"""dedup_suspects — advisory near-duplicate detector for the review surface.

The review queue already flags EXACT slug collisions at resolve time
(review_queue.enqueue -> reason 'collision', collision_with). This catches the
OTHER shape: a MINT-DRIFT near-dup — a fresh slug minted from a junk listing
title that is, in fact, an existing product. Example (2026-08-14): a demand
rescue minted `canon-eos-r6-v` (model "eos r6 v", no GTIN) which is just the
already-carded `canon-r6`. Different slug, so the exact-collision gate missed
it; it sailed to a build.

This is DELIBERATELY a soft, advisory signal, not a gate. Cross-repo dedup is
an irreducible human judgement (a real variant — R6 vs R6 II — looks almost
identical to a junk dup here), so the reviewer stays the decider. The badge
just makes the collision visible instead of silent. Recall over precision: it
would rather surface a harmless "these two are similar, sure you mean the right
one?" than let a true dup through unremarked.

Pure functions, no I/O — the caller supplies the universe of existing cards.
"""
from __future__ import annotations

import re

# Brand-line filler that carries no model-identity signal, stripped before the
# compare so "Canon EOS R6 V" and "Canon R6" both reduce to the R6 core. A
# variant marker ('ii'/'iii'/'v') is NOT filler — it may be a real distinct
# product, which is exactly why the result is advisory, not authoritative.
_FILLER = frozenset({
    "eos", "series", "camera", "body", "lens", "the", "with", "kit", "mm",
})
_TOK = re.compile(r"[a-z0-9]+")


def _brand_tokens(brand):
    return set(_TOK.findall((brand or "").lower()))


def model_tokens(brand, model):
    """The identity token set: model tokens, lowercased, minus filler and minus
    any token that merely repeats the brand (so 'Canon' in a model doesn't
    inflate the overlap with every other Canon)."""
    brand_toks = _brand_tokens(brand)
    return {
        t for t in _TOK.findall((model or "").lower())
        if t not in _FILLER and t not in brand_toks
    }


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def suspects(slug, brand, model, others, threshold=0.5):
    """Rank existing cards that this (slug, brand, model) may be a duplicate of.

    others: iterable of (slug, brand, model) for already-carded products.
    Returns [(other_slug, score), ...] sorted best-first, for SAME-BRAND cards
    whose model-token overlap (Jaccard) is >= threshold, excluding an exact
    slug/self match. Empty list when the model has no usable tokens (nothing to
    compare) or nothing clears the bar.

    Advisory only — see module docstring. Same-brand is required because a
    cross-brand token clash ('35mm', 'pro') is coincidence, not identity.
    """
    mine = model_tokens(brand, model)
    if not mine:
        return []
    my_brand = (brand or "").strip().lower()
    hits = []
    for other_slug, other_brand, other_model in others:
        if other_slug == slug:
            continue
        if (other_brand or "").strip().lower() != my_brand:
            continue
        score = _jaccard(mine, model_tokens(other_brand, other_model))
        if score >= threshold:
            hits.append((other_slug, round(score, 2)))
    hits.sort(key=lambda h: (-h[1], h[0]))
    return hits
