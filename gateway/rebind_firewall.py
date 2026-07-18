"""
rebind_firewall — sanity gate on resolve-path identity rebinds.
================================================================

Born from the 2026-07-16 live incident: the 04:07 resolve pass rebound
ulanzi-f38-zero to an "Empty Box" listing ($404.59 -> $25, MPN wiped,
affiliate URL selling a box). The disambiguator judges WHICH candidate
wins; this gate judges whether the winner is plausibly still the same
product. Reject posture: standing identity stays (stale-good beats
live-poison), suspect identity parks as an /admin receipt.

Reworked 2026-07-18 after the firewall's first live morning produced two
false positives (sony-a7c, sony-a7r): legitimate camera-body listings
hard-rejected because "W/Batts & Charger" tripped 'charger' in the old
bare-word junk list. Bundle language — what a listing INCLUDES — is the
norm in used markets and indistinguishable, at the single-word level,
from accessory-ONLY listings. The empirical dry-run over every listing
title in registry history (36 titles) drove this structure:

  HARD (any one rejects; all vertical-agnostic):
    - junk phrase:  multi-word not-the-product phrases ('empty box',
      'for parts', 'body cap' ...), substring-matched lowercase.
    - fatal word:   condition words that void the product regardless of
      vertical ('broken', 'faulty', 'replacement' ...), word-boundary.
    - relational:   'for/fits/compatible with' immediately preceding the
      STANDING identity's brand or model — "Charger for Sony A7C",
      "NEEWER Tripod ... for Peak Design", '"Empty Box" For the Ulanzi'.
      Product listings say "Tripod for Camera" (for a use); accessory and
      competitor listings say "X for <the product we stand on>". Caught
      the one specimen in registry history (a NEEWER competitor tripod
      filed under peak-design-travel-tripod) that no vocabulary list can.
      Anchors come from the standing entry, so a thin prior never
      manufactures suspicion — and no per-vertical vocabulary is needed.

  SOFT (any TWO reject; one alone is legitimate market noise):
    - price_collapse: fresh price < PRICE_COLLAPSE_RATIO x standing price
    - mpn_wiped:      standing MPN non-empty -> fresh MPN empty
    - category_flip:  standing eBay category id non-empty -> different id

  PAIRED (accessory vocabulary):
    - accessory_vocab ('charger', 'battery', 'case' ...) is evidence of
      accessory-ONLY solely when the price agrees: it rejects ONLY in
      combination with price_collapse. Bundles mention accessories at
      product prices (the 7/18 false-positive class: accessory word +
      wiped MPN at full price = legitimate rotation); accessory-only
      listings are definitionally cheap. This is the single per-vertical
      vocabulary, and it can never reject alone.

Vocabulary note: tools/refresh_used_prices.py carries its own
TITLE_BLACKLIST for the used-price precision gate (substring-matched).
Consolidating it onto this module's vocab is a deliberate follow-up
(7/16 decision), not done here.
"""
import re

# Multi-word phrases: substring match (lowercased).
JUNK_PHRASES = [
    'empty box', 'box only', 'for parts', 'parts only', 'as-is', 'as is',
    'not working', 'read description', 'screen protector', 'mount ring',
    'manual only', 'case only', 'body cap',
]

# Condition words that void the product in any vertical. Word-boundary.
FATAL_WORDS = [
    'broken', 'cracked', 'faulty', 'repair', 'replacement', 'defective',
]

# Accessory nouns: soft evidence only, and only alongside price_collapse.
# Word-boundary, so 'cap' never fires on 'Capture', 'skin' on 'Skinner'.
# This is the only per-vertical list; extend it when a vertical lands.
ACCESSORY_WORDS = [
    'cap', 'strap', 'manual', 'battery', 'charger', 'case', 'skin',
    'cover', 'plate', 'adapter', 'grip', 'hood', 'filter',
]

# Compatibility: tools/refresh_used_prices.py imports JUNK_WORDS to build
# its TITLE_BLACKLIST (substring gate on used-price comps). The union below
# reproduces the pre-2026-07-18 list exactly, so the nightly's behavior is
# byte-identical until the deliberate consolidation step (7/16 decision).
# New code must not use this name — it conflates fatal and accessory vocab.
JUNK_WORDS = FATAL_WORDS + ACCESSORY_WORDS

_FATAL_RES = [re.compile(r'\b' + re.escape(w) + r'\b') for w in FATAL_WORDS]
_ACC_RES = [re.compile(r'\b' + re.escape(w) + r'\b') for w in ACCESSORY_WORDS]

# 'for'/'fits'/'compatible with', optional 'the'/'my', then up to two
# filler words before the anchor must begin.
_REL_TEMPLATE = (r'\b(?:for|fits|compatible\s+with)\s+(?:the\s+|my\s+)?'
                 r'(?:[\w&.\-]+\s+){0,2}%s')

PRICE_COLLAPSE_RATIO = 0.30


def is_junk_title(title):
    """True if the title hard-reads as not-the-product: junk phrase or
    fatal condition word. Accessory nouns do NOT fire here (2026-07-18)."""
    t = (title or '').lower()
    if not t:
        return False
    if any(p in t for p in JUNK_PHRASES):
        return True
    return any(rx.search(t) for rx in _FATAL_RES)


def has_accessory_vocab(title):
    """True if the title carries accessory nouns (soft evidence only)."""
    t = (title or '').lower()
    return bool(t) and any(rx.search(t) for rx in _ACC_RES)


def _anchor_phrases(existing):
    """Brand/model phrases of the STANDING identity, for the relational
    rule. Model/alias variants (split on '/') count only when distinctive:
    multi-word, or containing a digit — 'a7c', 'f38', 'travel tripod' in;
    bare generic words like 'zero' out (they'd fire on ordinary prose)."""
    anchors = []
    brand = ((existing.get('identity') or {}).get('brand')
             or existing.get('vendor') or '').strip().lower()
    if brand:
        anchors.append(brand)
    raw = [existing.get('model') or '']
    raw += list(existing.get('aliases') or [])
    for item in raw:
        for variant in str(item).split('/'):
            v = variant.strip().lower()
            if not v:
                continue
            if ' ' in v or any(c.isdigit() for c in v):
                anchors.append(v)
    return anchors


def is_relational_reject(title, anchors):
    """True if the title offers something FOR the standing identity —
    'Charger for Sony A7C', 'NEEWER ... for Peak Design'. Anchors are
    matched as whole phrases inside a short window after the trigger, so
    'Tripod for Travel and Hiking' never fires on model 'Travel Tripod'."""
    t = re.sub(r'\s+', ' ', (title or '').lower())
    if not t:
        return False
    for a in anchors:
        pattern = _REL_TEMPLATE % re.escape(a).replace(r'\ ', r'\s+')
        if re.search(pattern, t):
            return True
    return False


def _price(entry):
    try:
        v = (entry.get('identity') or {}).get('price_seen', {}).get('value')
        return float(v) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


def _mpn(entry):
    return ((entry.get('identity') or {}).get('mpn') or '').strip()


def _category(entry):
    cats = entry.get('marketplace_categories') or {}
    cat = cats.get('ebay_category_id', '')
    if not cat:  # old-shape entries carry it inside identity
        cat = (entry.get('identity') or {}).get('ebay_category_id', '')
    return (cat or '').strip()


def assess(existing, entry):
    """Judge a rebind. Returns {'verdict': 'pass'|'reject',
    'hard': bool, 'signals': [str, ...]}.

    `existing` is the standing spine entry; `entry` is the fresh
    build_entry() output whose identity differs. Signals compare only
    where the standing entry actually carries the field — a thin prior
    never manufactures suspicion.
    """
    signals = []
    hard = False

    title = (entry.get('identity') or {}).get('market_title', '')
    if is_junk_title(title):
        signals.append('junk_title')
        hard = True
    if is_relational_reject(title, _anchor_phrases(existing)):
        signals.append('relational_for')
        hard = True

    old_p, new_p = _price(existing), _price(entry)
    if old_p and new_p is not None and new_p < old_p * PRICE_COLLAPSE_RATIO:
        signals.append('price_collapse')

    if _mpn(existing) and not _mpn(entry):
        signals.append('mpn_wiped')

    old_c, new_c = _category(existing), _category(entry)
    if old_c and new_c and new_c != old_c:
        signals.append('category_flip')

    if has_accessory_vocab(title):
        signals.append('accessory_vocab')

    core_soft = [s for s in signals
                 if s in ('price_collapse', 'mpn_wiped', 'category_flip')]
    accessory_pair = ('accessory_vocab' in signals
                      and 'price_collapse' in signals)
    verdict = 'reject' if (hard or len(core_soft) >= 2 or accessory_pair) \
        else 'pass'
    return {'verdict': verdict, 'hard': hard, 'signals': signals}
