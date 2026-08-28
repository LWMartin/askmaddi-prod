"""Shared query normalization for Lane A sources (Tier 1 misspelling tolerance).

Splits glued model+generation tokens so a user who omits the space still matches
titles that carry the spaced form:
    "a7iv"  -> "a7 iv"      "r6ii"   -> "r6 ii"
    "a7m4"  -> "a7 m4"      "z6iii"  -> "z6 iii"

Applied to the RAW query BEFORE either source (Adorama feed index, eBay Browse
API) filters on it — those sources match the literal query, so the split has to
happen upstream of them, not in precise.js (which only re-ranks rows already
returned). Mirrors the same split in browser/js/precise.js `tokens()`; keep the
two in sync.

Deterministic and vocabulary-free. Tier 2 (edit-distance typo correction against
the spine brand/model vocabulary — e.g. "sonny" -> "sony", and rewriting the eBay
query string) is a separate follow-up.
"""
from __future__ import annotations

import re

# A model root (letters then digits: a7, r6, z6, a7m…) glued to a generation
# suffix (roman numerals, m<n>, mark<n>). Longest alternatives first so "iii"
# wins over "ii". Deliberately EXCLUDES a bare trailing letter, so "a7r" is never
# split into "a7 r" — a7r is its own model, distinct from a7.
_GLUED = re.compile(r"^([a-z]*\d+)(iii|ii|iv|vi|v|mark\d+|m\d+)$", re.IGNORECASE)


def normalize_query(query):
    """Return the query with glued model tokens split. Non-glued tokens and word
    order are preserved. Idempotent (a already-spaced query is unchanged)."""
    if not query:
        return query
    out = []
    for tok in query.split():
        m = _GLUED.match(tok)
        if m:
            out.append(m.group(1))
            out.append(m.group(2))
        else:
            out.append(tok)
    return " ".join(out)
