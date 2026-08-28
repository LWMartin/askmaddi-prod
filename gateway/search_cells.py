"""Lane A precise-search Sieve cells — vendored into the gateway.

DUPLICATED (not imported) from phantom-ops aggregator-build/ingest/search_*.py
per the house "duplicate, don't import — phantom-ops is not a dependency"
doctrine (skus_registry.py:434). The pure logic is identical to the
factory-landed, holdout-gated cells; `extract_json` is inlined here (the gateway
has no claude.tools.forced_choice). See spec maddi-precise-search-lane-a.

Five rungs of a reverse-inference Sieve over live marketplace results:
  classify_result  Rung 0 — deterministic identity/marker classify
  rerank           Rung 1 — lexical coverage (+ optional injected embed)
  arbitrate        Rung 2 — forced-choice on the ambiguous residue (bounded)
  dedup_by_identity      — merge by resolved identity, 0.85 Jaccard tail
  compose                — canonical-first + capped compatible tail
"""
from __future__ import annotations

import json
import math
import re
from typing import Callable, Optional

# --- shared -----------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text):
    return _TOKEN_RE.findall((text or "").lower())


def _price_float_or(price, default):
    if price is None:
        return default
    digits = re.sub(r"[^0-9.]", "", str(price))
    if not digits:
        return default
    try:
        return float(digits)
    except ValueError:
        return default


def extract_json(raw):
    """Tolerant balanced-brace / fenced-JSON parse. None on anything unparseable
    (never a lucky partial) — the exclude-on-ambiguity guard. Mirrors
    claude.tools.forced_choice.extract_json."""
    if not raw:
        return None
    depth, start = 0, None
    for i, ch in enumerate(raw):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    obj = json.loads(raw[start:i + 1])
                except (ValueError, json.JSONDecodeError):
                    return None
                return obj if isinstance(obj, dict) else None
    return None


# --- Rung 0: classify -------------------------------------------------------

DEFAULT_ACCESSORY_MARKERS = frozenset({
    "for", "compatible", "fits", "replacement", "for use with", "aftermarket",
})


def _marker_pattern(marker):
    return r"\b" + r"\s+".join(re.escape(w) for w in marker.split()) + r"\b"


def classify_result(row, identity_lookup, *, markers=DEFAULT_ACCESSORY_MARKERS):
    if not isinstance(row, dict):
        row = {}
    sidecar = {k: row.get(k) for k in ("gtin", "mpn", "brand", "model")}
    try:
        identity_key = identity_lookup(sidecar)
    except Exception:
        identity_key = None
    if identity_key:
        return {"klass": "canonical", "identity_key": identity_key}
    name = row.get("name")
    if isinstance(name, str) and name and markers:
        try:
            for marker in markers:
                if marker and re.search(_marker_pattern(marker), name, re.IGNORECASE):
                    return {"klass": "accessory", "identity_key": None}
        except re.error:
            pass
    return {"klass": "ambiguous", "identity_key": None}


# --- Rung 1: MODEL-ANCHORED rerank ------------------------------------------
# Mirrors browser/js/precise.js score()/splitQuery()/modelMatch()/descriptorMatch()
# and the phantom-ops canonical ingest/search_rerank.py EXACTLY — one logic, three
# homes. Every model token in the query (a7, iv, r6, 90d, 50mm …) must EXACT
# whole-token match the title (the variant firewall: a7 != a7r, iv != iii);
# descriptors are soft with 1-edit typo tolerance; glued suffixes split ("a7iv" ->
# "a7 iv"); no-model queries fall back to descriptor coverage. See note
# 2026-08-28-askmaddi-positioning-precise-gear-research.

SEMANTIC_WEIGHT = 0.5

_STOPWORDS = frozenset({
    "under", "over", "below", "above", "less", "than", "with", "and", "the",
    "for", "best", "top", "cheap", "cheapest", "budget", "in", "on", "of", "a",
    "to", "or", "my", "me", "buy", "new", "used", "vs",
})

# Model root + glued generation marker ("a7iv" -> a7 + iv). Bare single-letter
# suffixes are NOT split, so "a7r" is never decomposed into "a7".
_GLUED_SUFFIX = re.compile(r"^([a-z]?[a-z]*\d+)(ii|iii|iv|vi|v|m\d+|mark\d+)$")
_ROMAN = frozenset({"ii", "iii", "iv", "v", "vi"})


def _tokens(s):
    """Tokenize AND split glued model suffixes (applied to queries and titles)."""
    out = []
    for t in _tokenize(s):
        m = _GLUED_SUFFIX.match(t)
        if m:
            out.append(m.group(1))
            out.append(m.group(2))
        else:
            out.append(t)
    return out


def _is_model_token(t):
    return (any(c.isalpha() for c in t) and any(c.isdigit() for c in t)) or t in _ROMAN


def _split_query(query):
    model, descriptor = [], []
    for t in _tokens(query):
        if t in _STOPWORDS or t.isdigit():
            continue  # stopword or pure-integer price/year noise: never a gate
        if _is_model_token(t):
            model.append(t)
        else:
            descriptor.append(t)
    return model, descriptor


def _within1(a, b):
    """Levenshtein <= 1: equal, or one insert/delete/substitute."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diff = 0
        for i in range(la):
            if a[i] != b[i]:
                diff += 1
                if diff > 1:
                    return False
        return diff == 1
    s, l = (a, b) if la < lb else (b, a)
    i = j = edits = 0
    while i < len(s) and j < len(l):
        if s[i] == l[j]:
            i += 1
            j += 1
        else:
            edits += 1
            if edits > 1:
                return False
            j += 1
    return True


def _model_match(qt, name_tokens):
    # EXACT whole-token — the variant firewall. No plural, no fuzz.
    return qt in name_tokens


def _descriptor_match(qt, name_tokens):
    for nt in name_tokens:
        if nt == qt or nt == qt + "s" or qt == nt + "s":
            return True
        if len(qt) >= 4 and len(nt) >= 4 and _within1(qt, nt):
            return True
    return False


def _score(model, descriptor, name):
    """precise.js score() — -1.0 to REJECT (missing a model token), else >= 0."""
    nt = _tokens(name)
    if not nt:
        return -1.0
    if model:
        for mt in model:
            if not _model_match(mt, nt):
                return -1.0
        s = float(len(model) * 10)
        for dt in descriptor:
            if _descriptor_match(dt, nt):
                s += 1.0
        return s
    hits = sum(1 for dt in descriptor if _descriptor_match(dt, nt))
    return float(hits) if hits > 0 else -1.0


def relevance(query, text):
    """Model-anchored relevance; -1.0 to reject, else >= 0. Shared with card
    matching so ranking and card surfacing use one logic (precise.js relevance())."""
    model, descriptor = _split_query(query)
    if not model and not descriptor:
        return -1.0
    return _score(model, descriptor, text)


# Kept for callers that pre-tokenize; now model-anchored via _score.
def _lexical_score(query_tokens, name):
    model, descriptor = [], []
    for t in query_tokens:
        if t in _STOPWORDS or t.isdigit():
            continue
        (model if _is_model_token(t) else descriptor).append(t)
    return _score(model, descriptor, name)


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def rerank(query, rows, *, embed=None):
    model, descriptor = _split_query(query)
    if (not model and not descriptor) or not rows:
        return []
    qvec = embed(query) if embed is not None else None
    scored = []
    for row in rows:
        score = _score(model, descriptor, row.get("name") or "")
        if score < 0.0:
            continue  # rejected: missing a required model token (variant firewall)
        if embed is not None and qvec is not None:
            score += SEMANTIC_WEIGHT * _cosine(qvec, embed(row.get("name") or ""))
        scored.append((score, _price_float_or(row.get("price"), float("inf")),
                       row.get("url") or "", row))
    scored.sort(key=lambda s: (-s[0], s[1], s[2]))
    return [row for _, _, _, row in scored]


# --- Rung 2: arbitrate (bounded residue) ------------------------------------

_VALID_KLASSES = {"canonical", "accessory"}


def _arb_system(query):
    return (f'You are ruling on a product search result for the query "{query}". '
            "Decide whether the listed product IS the canonical product named by "
            "the query, or is instead an accessory / third-party item for it. "
            'Reply as JSON with a `klass` field: "canonical", "accessory", or '
            '"uncertain" if you cannot tell.')


def arbitrate(query, rows, backend, *, cache=None):
    out = {}
    system = _arb_system(query)
    for row in rows:
        name = row["name"]
        if cache is not None and name in cache:
            out[row["url"]] = cache[name]
            continue
        obj = extract_json(backend(system, f"Product: {name}\nURL: {row['url']}"))
        klass = obj.get("klass") if obj else None
        out[row["url"]] = klass if klass in _VALID_KLASSES else "accessory"
    return out


# --- dedup by identity ------------------------------------------------------

_JACCARD_THRESHOLD = 0.85


def _words(name):
    return set(_TOKEN_RE.findall((name or "").lower()))


def _jaccard(a, b):
    wa, wb = _words(a), _words(b)
    if not wa and not wb:
        return 1.0
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _listing_sort_key(entry):
    price, row = entry
    if price is None:
        return (2, 0.0)
    return (0 if row.get("condition") == "New" else 1, price)


def _merge_group(identity_key, group_rows):
    parsed = [(_price_float_or(r.get("price"), None), r) for r in group_rows]
    ordered = sorted(parsed, key=_listing_sort_key)
    listings = [{"seller": r.get("seller"), "condition": r.get("condition"),
                 "price": r.get("price"), "currency": r.get("currency"),
                 "url": r.get("url"), "image": r.get("image")} for _, r in ordered]
    priced = [p for p, _ in parsed if p is not None]
    rep = ordered[0][1] if ordered else group_rows[0]
    return {
        "identity_key": identity_key,
        "name": rep.get("name"),
        "image": rep.get("image"),
        "condition": rep.get("condition"),
        "seller": rep.get("seller"),
        "url": rep.get("url"),
        "price": rep.get("price"),
        "listings": listings,
        "best_price": min(priced) if priced else None,
        "price_spread": (min(priced), max(priced)) if priced else (None, None),
    }


def dedup_by_identity(rows):
    if not rows:
        return []
    keyed, keyed_idx, keyless = {}, {}, []
    for idx, row in enumerate(rows):
        key = row.get("identity_key")
        if key:
            keyed.setdefault(key, [])
            keyed_idx.setdefault(key, idx)
            keyed[key].append(row)
            continue
        name = row.get("name")
        for cl in keyless:
            if _jaccard(name, cl["rep"]) >= _JACCARD_THRESHOLD:
                cl["rows"].append(row)
                break
        else:
            keyless.append({"rep": name, "rows": [row], "idx": idx})
    merged = [(keyed_idx[k], _merge_group(k, g)) for k, g in keyed.items()]
    merged += [(cl["idx"], _merge_group(None, cl["rows"])) for cl in keyless]
    merged.sort(key=lambda p: p[0])
    return [r for _, r in merged]


# --- compose ----------------------------------------------------------------

def compose(canonical, accessory, *, tail_cap=8):
    acc_count = min(len(accessory), tail_cap)
    results = canonical + accessory[:acc_count]
    sections = ([{"label": "Products", "count": len(canonical)},
                 {"label": "Compatible & third-party", "count": acc_count}]
                if results else [])
    return {"results": results, "sections": sections,
            "dropped_tail": max(0, len(accessory) - tail_cap)}
