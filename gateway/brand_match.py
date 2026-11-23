"""brand_match.py — one definition of "this brand can stand for that vendor".

Moved out of resolve_sku (the 2026-11-18 mint brand guard) on 2026-11-23 so
slug_normalizer's registry-name lookup can apply the same maker equivalence
without importing resolve_sku (which imports slug_normalizer). resolve_sku
re-exports `_mint_brand_agrees` under its old name; behaviour is unchanged.
"""
import re

_BRAND_PLACEHOLDERS = frozenset({
    'unbranded', 'generic', 'doesnotapply', 'dosenotapply', 'na', 'none',
    'unknown', 'notapplicable', 'nobrand', 'oem'})
# Renames / trading names of ONE maker: either side may use any form.
_BRAND_SAME = (frozenset({'fujifilm', 'fuji'}),
               frozenset({'olympus', 'omsystem', 'omdigital', 'omdigitalsolutions'}))


def _brand_words(b):
    return set(re.findall(r'[a-z0-9]+', str(b or '').lower()))


def mint_brand_agrees(vendor, listing_brand):
    """True when the listing's brand can stand for the vendor. An EMPTY brand is
    no evidence (the extractor found none; the card falls back to the vendor) and
    passes; an explicit placeholder ('Unbranded', 'Does not apply') does NOT -
    it is what rendered as the PowerShot's brand. Agreement is word-level ('Paul
    C Buff' / 'Paul C. Buff'; 'INSTA360 GO3' / 'Insta360') or a known rename."""
    b = str(listing_brand or '').strip()
    if not b:
        return True
    nb = re.sub(r'[^a-z0-9]', '', b.lower())
    if nb in _BRAND_PLACEHOLDERS:
        return False
    nv = re.sub(r'[^a-z0-9]', '', str(vendor or '').lower())
    if not nv:
        return True
    if any(nv in g and nb in g for g in _BRAND_SAME):
        return True
    wv, wb = _brand_words(vendor), _brand_words(b)
    return wv <= wb or wb <= wv


def same_maker(a, b):
    """Strict both-sides form for identity lookups: two NON-EMPTY vendor names
    name the same maker (word-level containment or a known rename). Unlike
    mint_brand_agrees, an empty side is NOT agreement — a lookup that cannot
    show the maker matches must not resolve to another maker's card."""
    na = re.sub(r'[^a-z0-9]', '', str(a or '').lower())
    nb = re.sub(r'[^a-z0-9]', '', str(b or '').lower())
    if not na or not nb:
        return False
    return mint_brand_agrees(a, b)
