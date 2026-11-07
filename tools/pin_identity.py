"""
pin_identity — is the product a new-price pin points at actually THIS card's product?

The matcher (phantom-ops ingest/adorama_match.match_target) joins a card's SPINE
identity (gtin / brand+mpn) to New, in-stock feed rows. It is faithful to that
identity — so when the identity itself is wrong, it pins the wrong product with
full confidence. Two live false prices proved it (removed 2026-11-05):

  canon-r6        pinned "canon-eos-r6-mark-ii-…/p/car6m2" ($1999): the card's eBay
                  listing sold, resolve re-stamped the spine MPN from the next
                  listing (an R6 Mark II), and the matcher priced the Mark II.
  canon-eos-550d  pinned "used-canon-eos-2000d-…-rebel-t7-…/p/us2087889" ($420):
                  a contaminated identity pointed at a T7 kit, and the feed itself
                  labelled that USED product condition "N", so the New-only filter
                  let it through.

So this check does not trust the spine, nor the feed's condition flag. It compares
the pinned product (its URL slug, plus the feed title when there is one) with the
card's OWN `identity.model` — the name the page shows the reader — and refuses:

  used       the product is used / refurbished / open-box / pre-owned / demo
             (slug word, title word, or an Adorama used product code `us<digits>`);
  model      a digit-bearing atom of the card's model (e.g. "550d", "r5", "200mm",
             "f2.8") is not present in the product as a WHOLE run of units — so
             "r5" is not found inside "r50", nor "x100v" inside "x100vi";
  generation the roman generation markers differ (card "R6" vs product "R6 Mark II";
             card "R6 II" vs product "R6"). "Mark"/"Mk" is filler: "R6 II" ==
             "R6 Mark II".

Units: text is lower-cased and split into letter-runs and digit-runs (a decimal
point is a separator, so "f2.8" and "f-2-8" are both [f, 2, 8]). A parenthetical
in the model ("A7 IV (ILCE-7M4)") is an alias, not a required atom.

A failing check is evidence about the PINNED PRODUCT, never a guess: the caller
holds the price (never writes it) and reports why. The fix for a card whose name
under-specifies its product (e.g. a generation the spine MPN carries but the name
omits) is in the card's data, not a looser check — same ruling as 2026-11-05's
correct_brand door ("wrong brand fixed in data, keeps the strict guard").

Pure, stdlib only, py3.9-safe (cron runs /usr/bin/python3 = 3.9.25).
"""

from __future__ import annotations

import re
import urllib.parse
from typing import List, Optional, Tuple

GENERATIONS = {"ii", "iii", "iv", "v", "vi", "vii", "viii"}
FILLER = {"mark", "mk"}
_USED_WORDS = ("used", "refurbished", "refurb", "open-box", "openbox", "open box",
               "pre-owned", "preowned", "demo")
_USED_SLUG = re.compile(r"(^|-)(used|refurbished|refurb|open-box|openbox|pre-owned|preowned|demo)(-|$)")
_USED_TITLE = re.compile(r"\b(used|refurbished|refurb|open[ -]?box|pre-?owned|demo)\b")
_USED_CODE = re.compile(r"^us\d")


def destination(url: Optional[str]) -> Tuple[str, str]:
    """(slug, product_code) of a — possibly prf.hn-wrapped — Adorama product URL."""
    u = urllib.parse.unquote(url or "")
    d = u.split("destination:", 1)[-1].split("?", 1)[0]
    parts = [p for p in urllib.parse.urlparse(d).path.split("/") if p]
    slug = parts[0].lower() if parts else ""
    code = parts[-1].lower() if len(parts) >= 3 and parts[-2] == "p" else ""
    return slug, code


# Spelled-out forms a retailer slug uses for a model letter the card abbreviates.
# Sony writes "A1"/"A7 IV" as "Alpha 1"/"Alpha A7 IV" (adorama: sony-alpha-1-…).
# Mapping only ever NARROWS to the card's own letter; it never adds a number.
UNIT_ALIASES = {"alpha": "a"}


def units(text: Optional[str]) -> List[str]:
    """Letter-runs and digit-runs, lower-cased; filler words dropped; aliases folded."""
    out = []
    for u in re.findall(r"[a-z]+|[0-9]+", (text or "").lower()):
        if u in FILLER:
            continue
        out.append(UNIT_ALIASES.get(u, u))
    return out


def _model_atoms(model: str) -> List[List[str]]:
    """Each whitespace/hyphen word of the model that carries a digit, as units."""
    m = re.sub(r"\(.*?\)", " ", model or "").lower().replace("f/", "f")
    words = re.findall(r"[a-z0-9.]+", m)
    return [units(w) for w in words if re.search(r"\d", w)]


def _contains_run(hay: List[str], run: List[str]) -> bool:
    n = len(run)
    return n > 0 and any(hay[i:i + n] == run for i in range(len(hay) - n + 1))


def _generations(us: List[str]) -> set:
    return {u for u in us if u in GENERATIONS}


def card_model(card: dict) -> str:
    """The name to check a pin against: identity.display_name — what the reader
    sees, and where hand corrections land (spine `overrides.display_name`; the
    11-05 renames "GoPro MAX 2" / "DJI Osmo Action 5 Pro" changed ONLY that, their
    `model` still reads "max 8k 360 action" / "osmo action 5"). identity.model is
    the fallback. Brand words carry no digit/roman unit, so the brand prefix adds
    no atoms. Never identity.mpn — on 2026-11-06 the canon-r6 card's mpn read
    'Canon EOS R6 Mark III' (an eBay title), a third model."""
    ident = (card or {}).get("identity") or {}
    return (ident.get("display_name") or ident.get("model") or "").strip()


def check_pin(model: str, url: Optional[str], title: Optional[str] = None) -> List[str]:
    """Reasons the pinned product is NOT the card's product; [] means it is.

    `model` is the card's identity.model (fall back to display_name minus brand at
    the call site); `url` the pin's affiliate/product URL; `title` the feed title
    when the caller has it (the sheet does; an existing card pin does not)."""
    slug, code = destination(url)
    reasons = []
    t = (title or "").lower()
    if _USED_SLUG.search(slug) or _USED_TITLE.search(t) or _USED_CODE.match(code):
        reasons.append("used: pinned product is used/refurbished/open-box")

    if not (model or "").strip():
        reasons.append("model: card has no identity.model to check against")
        return reasons

    sides = [units(slug)] + ([units(title)] if title else [])
    missing = []
    for atom in _model_atoms(model):
        if not any(_contains_run(s, atom) for s in sides):
            missing.append("".join(atom))
    if missing:
        reasons.append(f"model: {', '.join(missing)} not in pinned product")

    card_gen = _generations(units(re.sub(r"\(.*?\)", " ", model)))
    prod_gen = _generations(units(slug))
    if title:
        # The slug is the product's canonical name; a title may add marketing
        # words but must not contradict it — take the slug's generation, and the
        # title's only when the slug carries none.
        prod_gen = prod_gen or _generations(units(title))
    if card_gen != prod_gen:
        extra, lacking = sorted(prod_gen - card_gen), sorted(card_gen - prod_gen)
        bits = []
        if extra:
            bits.append(f"product is {'/'.join(extra).upper()}, card is not")
        if lacking:
            bits.append(f"card is {'/'.join(lacking).upper()}, product is not")
        reasons.append("generation: " + "; ".join(bits))
    return reasons
