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

_lock = threading.Lock()
_cache = {"mtime": None, "rows": [], "names": []}


def _tokenize(text):
    return _TOKEN_RE.findall((text or "").lower())


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
    qtokens = _tokenize(query)
    if not qtokens:
        return []
    rows, names = _load()
    out = []
    for row, ntokens in zip(rows, names):
        if not ntokens:
            continue
        if not all(any(qt in nt for nt in ntokens) for qt in qtokens):
            continue
        coverage = sum(1 for nt in ntokens if any(qt in nt for qt in qtokens)) / len(ntokens)
        out.append((coverage, row))
    out.sort(key=lambda c: -c[0])
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
        results.append({
            "name": name,
            "price": row.get("price"),
            "currency": "USD",
            "image": "",  # snapshot dropped image_url; re-include is a fast-follow
            "url": row.get("url"),
            "condition": "New",
            "seller": "Adorama",
            "gtin": row.get("gtin"),
            "mpn": row.get("mpn"),
            "brand": brand,
            "model": model,
        })
    return results
