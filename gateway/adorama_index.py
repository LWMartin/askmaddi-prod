"""Adorama search source for Lane A — free-text over the local feed index.

Loads the compact in-stock index (data/adorama-search-index.json, produced by
phantom-ops ingest.adorama_search_index from the nightly snapshot) and answers
`search(query, limit)` with eBay-parity rows so the frontend renders Adorama
(New) beside eBay (Used) through one code path. The index is loaded once and
cached, refreshed when its mtime changes (the nightly rewrites it atomically).

Rows carry the identity sidecar {gtin, mpn, brand, model} so Lane A's classify
rung can resolve them; url is the already-Partnerize-wrapped feed link (never
re-wrapped).
"""
from __future__ import annotations

import json
import os
import re
import threading

_INDEX_PATH = os.environ.get(
    "ADORAMA_SEARCH_INDEX",
    os.path.join(os.path.dirname(__file__), "..", "data", "adorama-search-index.json"))
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Filler/qualifier words that appear in natural-language queries but never in a
# product title ("travel tripod UNDER $400", "sony full-frame camera"). Dropped
# from the query so they don't starve the match. Pure digits are dropped too
# (price/qualifier numbers like "400"); real model numbers keep their letters.
_STOPWORDS = frozenset({
    "under", "over", "below", "above", "less", "than", "with", "and", "the",
    "for", "best", "top", "cheap", "cheapest", "budget", "in", "on", "of", "a",
    "to", "or", "my", "me", "buy", "new", "used", "vs",
})

_lock = threading.Lock()
_cache = {"mtime": None, "rows": [], "names": []}


def _tokenize(text):
    return _TOKEN_RE.findall((text or "").lower())


def _query_tokens(query):
    """Content tokens of a query: drop stopwords and pure-number qualifiers."""
    return [t for t in _tokenize(query) if t not in _STOPWORDS and not t.isdigit()]


def _matches(qt, ntokens):
    """True if `qt` matches some name token with singular/plural tolerance
    ("tripods"~"tripod", "lenses"~"lens") — but NEVER a substring merge, so "a7"
    never matches "a7r". All O(1) SET lookups against the pre-tokenized name set
    (no per-name-token iteration) — this is the hot path over ~130k rows.

    Semantically identical to the old `qt in nt or any(_tmatch(qt,n))`: the five
    _tmatch cases become direct membership tests (nt==qt+s/es, qt==nt+s/es)."""
    if qt in ntokens:
        return True
    if qt + "s" in ntokens or qt + "es" in ntokens:      # a name token is qt + s/es
        return True
    if qt.endswith("es") and qt[:-2] in ntokens:         # qt is name token + es
        return True
    if qt.endswith("s") and qt[:-1] in ntokens:          # qt is name token + s
        return True
    return False


def _score(qtokens, ntokens):
    """How many query tokens match some name token (exact fast-path, then plural)."""
    return sum(1 for qt in qtokens if _matches(qt, ntokens))


def is_configured():
    return os.path.exists(_INDEX_PATH)


def _load():
    """(Re)load the index if the file changed. Returns (rows, pretokenized_names)."""
    try:
        mtime = os.path.getmtime(_INDEX_PATH)
    except OSError:
        return [], []
    with _lock:
        if _cache["mtime"] == mtime:
            return _cache["rows"], _cache["names"]
        with open(_INDEX_PATH, encoding="utf-8") as fh:
            rows = json.load(fh).get("rows", [])
        names = [set(_tokenize(f"{r.get('brand','')} {r.get('model','')}")) for r in rows]
        _cache.update(mtime=mtime, rows=rows, names=names)
        return rows, names


def search(query, limit=25):
    """Free-text AND-token search over the in-stock index; parity rows out.

    Every query token must substring-hit some name token (AND, no OR leakage —
    mirrors the Lane A rerank gate). Returns up to `limit` rows; final ranking is
    Lane A's rerank cell, so here we just filter and cap by a cheap coverage sort.
    """
    qtokens = _query_tokens(query)
    if not qtokens:
        return []
    rows, names = _load()
    out = []
    for row, ntokens in zip(rows, names):
        if not ntokens:
            continue
        s = _score(qtokens, ntokens)
        if s == 0:                       # no query token matched → not a result
            continue
        # rank: most query tokens matched first, then tighter name (less noise)
        out.append(((s, -len(ntokens)), row))
    out.sort(key=lambda c: (-c[0][0], -c[0][1]))
    results = []
    for _, row in out[:limit]:
        brand = row.get("brand", "")
        model = row.get("model", "")
        # The feed's `model` often already leads with the brand ("Sony Sony …") —
        # don't double it in the display name.
        if brand and model.lower().startswith(brand.lower()):
            name = model
        else:
            name = f"{brand} {model}".strip()
        # The feed sometimes doubles a leading word ("Sony Sony UTX-P1"); collapse
        # any run of identical consecutive words to one.
        name = re.sub(r"\b(\w+)(\s+\1\b)+", r"\1", name, flags=re.IGNORECASE)
        results.append({
            "name": name,
            "price": row.get("price"),
            "currency": "USD",
            "image": row.get("image") or "",
            "url": row.get("url"),
            "condition": "New",
            "seller": "Adorama",
            "gtin": row.get("gtin"),
            "mpn": row.get("mpn"),
            "brand": brand,
            "model": model,
        })
    return results
