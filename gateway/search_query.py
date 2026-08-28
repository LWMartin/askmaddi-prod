"""Shared query normalization for Lane A sources (misspelling tolerance).

Applied to the RAW query BEFORE either source (Adorama feed index, eBay Browse
API) filters on it — those sources match the literal query, so a fix has to
happen upstream of them, not in precise.js (which only re-ranks rows already
returned). `normalize_query` runs two rungs in order:

  Tier 1 — GLUED SPLIT (deterministic, vocabulary-free):
    "a7iv" -> "a7 iv"   "r6ii" -> "r6 ii"   "a7m4" -> "a7 m4"   "z6iii" -> "z6 iii"

  Tier 2 — TYPO CORRECTION (edit-distance 1 against a brand vocabulary):
    "sonny" -> "sony"   "canan" -> "canon"   "fujifim" -> "fujifilm"

Tier 2 SAFETY (correct by construction, never a guess):
  - snap-to vocabulary = brand tokens from the Adorama in-stock index, brands with
    >= 5 listings only (long-tail/obscure brands dropped so a typo can't snap to junk);
  - a query token is PROTECTED (never corrected) if it already appears in the
    product corpus (any brand/model token seen >= 3 times) — "wide", "macro",
    "angle" are real lens words, so they never mis-snap to a near brand ("wine",
    "micro"); only a token ABSENT from the corpus is a candidate typo. This is
    self-maintaining from the index — no hand-curated denylist;
  - only NON-model tokens are corrected — a token with a digit (a7, r6, 50mm) or a
    roman generation marker (ii..vi) is NEVER touched, so a7 can't become a7r and
    the variant firewall in precise.js holds;
  - only tokens of length >= 4 (short tokens have too-dense 1-edit neighbourhoods);
  - only when it has EXACTLY ONE edit-distance-1 brand match (0 or >=2 -> left alone).

Mirrors the glued split in browser/js/precise.js `tokens()`; keep in sync. (Typo
correction is upstream-only — precise.js never sees the corrected form.)
"""
from __future__ import annotations

import json
import os
import re
import threading

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_ROMAN = frozenset({"ii", "iii", "iv", "v", "vi"})

# A model root (letters then digits: a7, r6, z6) glued to a generation suffix
# (roman numerals, m<n>, mark<n>). Longest alternatives first so "iii" wins over
# "ii". Deliberately EXCLUDES a bare trailing letter, so "a7r" is never split into
# "a7 r" — a7r is its own model, distinct from a7.
_GLUED = re.compile(r"^([a-z]*\d+)(iii|ii|iv|vi|v|mark\d+|m\d+)$", re.IGNORECASE)

# --- Tier 2 vocabulary (brand tokens from the Adorama index) ----------------

_INDEX_PATH = os.environ.get(
    "ADORAMA_SEARCH_INDEX",
    os.path.join(os.path.dirname(__file__), "..", "data", "adorama-search-index.json"))
_MIN_BRAND_COUNT = 5   # drop long-tail brands so a typo can't snap to obscure junk
_MIN_KNOWN_COUNT = 3   # a token seen this often in the corpus is a real word (protected)
_MIN_CORRECT_LEN = 4   # short tokens have too many 1-edit neighbours to correct safely

_vlock = threading.Lock()
# targets: brand tokens we may snap a typo TO. known: every corpus token (brand or
# model) seen often enough to be a real word — protected from correction.
_vocab_cache = {"mtime": None, "targets": frozenset(), "known": frozenset()}


def _load_vocab():
    """(targets, known) token sets from the Adorama index, cached and refreshed on
    the index's mtime (the nightly rewrites it atomically). Empty if absent."""
    try:
        mtime = os.path.getmtime(_INDEX_PATH)
    except OSError:
        return frozenset(), frozenset()
    with _vlock:
        if _vocab_cache["mtime"] == mtime:
            return _vocab_cache["targets"], _vocab_cache["known"]
        brand_counts, token_counts = {}, {}
        try:
            with open(_INDEX_PATH, encoding="utf-8") as fh:
                for row in json.load(fh).get("rows", []):
                    brand = (row.get("brand") or "").strip().lower()
                    if brand:
                        brand_counts[brand] = brand_counts.get(brand, 0) + 1
                    text = f"{brand} {(row.get('model') or '').lower()}"
                    for tok in _TOKEN_RE.findall(text):
                        token_counts[tok] = token_counts.get(tok, 0) + 1
        except (OSError, ValueError):
            return _vocab_cache["targets"], _vocab_cache["known"]
        targets = set()
        for brand, count in brand_counts.items():
            if count < _MIN_BRAND_COUNT:
                continue
            for tok in _TOKEN_RE.findall(brand):
                if len(tok) >= _MIN_CORRECT_LEN and not any(c.isdigit() for c in tok):
                    targets.add(tok)
        known = frozenset(t for t, c in token_counts.items() if c >= _MIN_KNOWN_COUNT)
        targets = frozenset(targets)
        _vocab_cache.update(mtime=mtime, targets=targets, known=known)
        return targets, known


def _within1(a, b):
    """Levenshtein distance <= 1 (equal / one insert / delete / substitute)."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:                                   # one substitution
        diff = 0
        for x, y in zip(a, b):
            if x != y:
                diff += 1
                if diff > 1:
                    return False
        return diff == 1
    s, l = (a, b) if la < lb else (b, a)           # one insert/delete
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


def _is_model_token(tok):
    return any(c.isdigit() for c in tok) or tok in _ROMAN


# --- Tier 1: glued split ----------------------------------------------------

def _split_glued(query):
    out = []
    for tok in query.split():
        m = _GLUED.match(tok)
        if m:
            out.append(m.group(1))
            out.append(m.group(2))
        else:
            out.append(tok)
    return out  # list of tokens


# --- Tier 2: typo correction ------------------------------------------------

def _correct(tokens):
    targets, known = _load_vocab()
    if not targets:
        return tokens
    out = []
    for tok in tokens:
        low = tok.lower()
        if (len(low) >= _MIN_CORRECT_LEN and not _is_model_token(low)
                and low not in known):                 # absent from corpus = candidate typo
            matches = [v for v in targets if _within1(low, v)]
            if len(matches) == 1:
                out.append(matches[0])
                continue
        out.append(tok)
    return out


def normalize_query(query):
    """Glued-split then typo-correct the raw query. Non-glued, non-typo tokens and
    word order are preserved. Idempotent."""
    if not query:
        return query
    return " ".join(_correct(_split_glued(query)))
