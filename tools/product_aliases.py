"""
product_aliases — the other names a reader types for the product a card is about.

Why (2026-11-17): Search Console showed the a7 IV page being searched as
"a74", "a7 4", "a7 m4", "a7 iv" — none of which the page carried except the
last. Google doesn't read meta keywords and treats hidden text as spam, so the
honest lever is to SAY the names: a small visible "Also known as" line under
the title, plus schema.org `alternateName` on the Product.

Every alias is DERIVED from the card's own name, its part number, or a
hand-authored `identity.sku_alt_names` entry — never invented:

  numeral    a body's trailing generation numeral is written both ways and
             both spacings: "A7 IV" -> "A7 IV", "A7IV", "A7 4", "A74".
             Bodies only — on a lens a trailing "II" is a line suffix
             ("GM OSS II"), and "OSS 2" is nobody's name for it.
  mark       "Mark" is filler (same ruling as pin_identity: "R6 II" ==
             "R6 Mark II"), so "EOS R6 Mark III" also yields "EOS R6 III".
  part no.   Sony's "M4" convention is the part number's own spelling
             (ILCE-7M4 -> "A7 M4", "A7M4"). Emitted ONLY when the MPN carries
             the model's digits + "M" + the SAME generation as a whole run —
             so a wrong spine MPN (sony-a7-v carries the A7R V's ILCE7RM5B)
             yields nothing: "7M5" is not a run inside "ILCE7RM5B". A wrong
             MPN can only pass by spelling this exact product.
  stored     identity.sku_alt_names (hand-authored), and a parenthetical in
             the display name ("(ILCE-7M4)").

Two names are the SAME alias when they match after lower-casing and dropping
everything but letters and digits ("A7 4" == "a74"). The visible line shows one
readable (spaced) spelling per distinct name and skips names the title already
shows; `alternateName` carries every spelling.

catalog_aliases() drops any alias that collides with ANOTHER card's name or
alias — an alias must point at one product or it is not an alias.

Pure, stdlib only, py3.9-safe (cron runs /usr/bin/python3 = 3.9.25).
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional

ROMAN = {"II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8}
_GEN = re.compile(r"^(?P<prefix>.*?\S)\s+(?P<mark>Mark\s+)?(?P<roman>II|III|IV|V|VI|VII|VIII)$",
                  re.IGNORECASE)
_PAREN = re.compile(r"\(([^)]*)\)")
BODY_CATEGORIES = {"body", "camera"}


def key(name: str) -> str:
    """Identity key of a spelling: lower-case letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _strip_brand(name: str, brand: str) -> str:
    if brand and name.lower().startswith(brand.lower() + " "):
        return name[len(brand) + 1:].strip()
    return name.strip()


def _mpn_m_forms(prefix: str, n: int, mpn: Optional[str]) -> List[str]:
    """Sony-style 'A7 M4' forms, only when the MPN itself spells them."""
    if not mpn:
        return []
    # Model digits must START the MPN once its letter prefix is dropped
    # ("ILCE7M4" -> "7M4", "DSCRX10M5" -> "10M5"): "RX10" can't match inside
    # "RX100M5", and "7" can't match "7RM5" (the A7R V) as "7M5".
    body = re.sub(r"[^A-Z0-9]", "", mpn.upper())
    rest = re.sub(r"^[A-Z]+", "", body)
    tail = re.sub(r"^[A-Z]+", "", re.sub(r"[^A-Z0-9]", "", prefix.upper()))
    if not tail or not re.search(r"\d", tail):
        return []
    if not re.match(rf"{re.escape(tail)}M{n}(?![0-9])", rest):
        return []
    return [f"{prefix} M{n}", f"{prefix}M{n}"]


def derive_aliases(display_name: str, brand: str = "", mpn: Optional[str] = None,
                   category: str = "", stored: Iterable[str] = ()) -> List[str]:
    """All alias spellings for one card, brand-less, title order, deduped by
    exact spelling. The display name's own spelling is not included."""
    out: List[str] = []

    def add(s: str) -> None:
        s = re.sub(r"\s+", " ", (s or "")).strip()
        if s and s not in out:
            out.append(s)

    for p in _PAREN.findall(display_name or ""):
        add(p)
    core = _strip_brand(_PAREN.sub("", display_name or "").strip(), brand)

    if (category or "").lower() in BODY_CATEGORIES:
        m = _GEN.match(core)
        if m:
            prefix, roman = m.group("prefix"), m.group("roman").upper()
            n = ROMAN[roman]
            if m.group("mark"):
                add(f"{prefix} Mark {n}")
            add(f"{prefix} {roman}")
            add(f"{prefix}{roman}")
            add(f"{prefix} {n}")
            add(f"{prefix}{n}")
            for f in _mpn_m_forms(prefix, n, mpn):
                add(f)

    for s in stored or ():
        add(_strip_brand(s, brand))

    return [a for a in out if a != core]


def visible_aliases(display_name: str, aliases: List[str], brand: str = "") -> List[str]:
    """One readable spelling per distinct name, skipping names the title shows.
    First spelling wins, and derive_aliases lists spaced forms first."""
    shown = {key(_strip_brand(_PAREN.sub("", display_name or ""), brand))}
    shown |= {key(p) for p in _PAREN.findall(display_name or "")}
    picked, seen = [], set(shown)
    for a in aliases:
        k = key(a)
        if k and k not in seen:
            seen.add(k)
            picked.append(a)
    return picked


def card_aliases(card: dict, mpn: Optional[str] = None) -> List[str]:
    ident = card.get("identity") or {}
    return derive_aliases(ident.get("display_name", ""), ident.get("brand", ""),
                          mpn, ident.get("category", ""), ident.get("sku_alt_names") or ())


def catalog_aliases(cards: Iterable[dict], mpns: Optional[Dict[str, str]] = None) -> Dict[str, List[str]]:
    """{card_id: aliases} with cross-card collisions removed from every card
    involved (by key). A card's own title never collides with itself."""
    mpns = mpns or {}
    cards = list(cards)
    per: Dict[str, List[str]] = {}
    owners: Dict[str, set] = {}
    for c in cards:
        cid = c.get("card_id")
        ident = c.get("identity") or {}
        brand = ident.get("brand", "")
        al = card_aliases(c, mpns.get(cid))
        per[cid] = al
        title_k = key(brand + _strip_brand(_PAREN.sub("", ident.get("display_name", "")), brand))
        for k in {title_k} | {key(brand + a) for a in al}:
            owners.setdefault(k, set()).add(cid)
    out = {}
    for c in cards:
        cid = c.get("card_id")
        brand = (c.get("identity") or {}).get("brand", "")
        out[cid] = [a for a in per[cid] if owners.get(key(brand + a), {cid}) == {cid}]
    return out
