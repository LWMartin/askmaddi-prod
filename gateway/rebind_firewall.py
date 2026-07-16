"""
rebind_firewall — sanity gate on identity REBINDS in the resolve path.
======================================================================
Found live 2026-07-16: the 04:07 resolve rebound ulanzi-f38-zero to an
'"Empty Box" For the Ulanzi Coman Zero F38' listing — $404.59 -> $25.00,
MPN wiped, category flipped, affiliate URL pointed at an empty box. The
disambiguator's confidence verdict guards WHICH candidate wins; nothing
guarded whether a REBIND (identity change against a standing spine entry)
is plausibly still the same product. This module is that guard.

Scope: assess() runs ONLY when a standing entry exists and the fresh
identity differs (_same_identity false) — first resolves and idempotent
re-resolves never touch it. Verdict semantics follow the repo's fail
posture (design-principle-exclude-on-ambiguity-when-claim-rich): on
reject, the standing identity STAYS on the spine, the fresh identity is
parked as a receipt for /admin, and the resolve returns outcome
'rebind_rejected' — loud, never silent.

Rules:
  HARD (any one rejects):
    - junk title: the fresh market_title carries junk-listing vocabulary
      (empty box, for parts, accessory-only words). Word-boundary matched
      so 'cap' never fires on 'Capture'.
  SOFT (any TWO reject; one alone is legitimate market noise):
    - price_collapse: fresh price < PRICE_COLLAPSE_RATIO x standing price
    - mpn_wiped:      standing MPN non-empty -> fresh MPN empty
    - category_flip:  standing eBay category id non-empty -> different id

Vocabulary note: tools/refresh_used_prices.py carries its own
TITLE_BLACKLIST for the used-price precision gate (substring-matched).
This list is the superset and the intended canonical home; consolidating
the nightly's copy onto it is a deliberate follow-up, not done here —
we don't edit the nightly script the day its 3-week deadlock cleared.
"""
import re

# Multi-word phrases: substring match (lowercased).
JUNK_PHRASES = [
    'empty box', 'box only', 'for parts', 'parts only', 'as-is', 'as is',
    'not working', 'read description', 'screen protector', 'mount ring',
    'manual only', 'case only', 'body cap',
]

# Single words: word-boundary match, so 'cap' != 'Capture', 'skin' != 'Skinner'.
JUNK_WORDS = [
    'broken', 'cracked', 'faulty', 'repair', 'replacement', 'defective',
    'cap', 'strap', 'manual', 'battery', 'charger', 'case', 'skin',
    'cover', 'plate', 'adapter', 'grip', 'hood', 'filter',
]

_WORD_RES = [re.compile(r'\b' + re.escape(w) + r'\b') for w in JUNK_WORDS]

PRICE_COLLAPSE_RATIO = 0.30


def is_junk_title(title):
    """True if the title reads as a not-the-product listing."""
    t = (title or '').lower()
    if not t:
        return False
    if any(p in t for p in JUNK_PHRASES):
        return True
    return any(rx.search(t) for rx in _WORD_RES)


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

    old_p, new_p = _price(existing), _price(entry)
    if old_p and new_p is not None and new_p < old_p * PRICE_COLLAPSE_RATIO:
        signals.append('price_collapse')

    if _mpn(existing) and not _mpn(entry):
        signals.append('mpn_wiped')

    old_c, new_c = _category(existing), _category(entry)
    if old_c and new_c and new_c != old_c:
        signals.append('category_flip')

    soft = [s for s in signals if s != 'junk_title']
    verdict = 'reject' if (hard or len(soft) >= 2) else 'pass'
    return {'verdict': verdict, 'hard': hard, 'signals': signals}
