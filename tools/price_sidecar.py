"""
price_sidecar.py — captured used-market prices, OUTSIDE the tracked card spine.
==============================================================================
The used-price refresh job re-queries eBay daily and produces fresh price bands.
Those prices are CAPTURED state (they originate on the box, from live market
data), not AUTHORED state (which the repo owns and flows down to the box). They
were previously written INTO each card JSON's `pricing.used_market` block —
which dirtied the tracked spine on every refresh and risked a git-pull conflict
on the box (observed 2026-06-25: eight tracked files modified locally by the
daily refresh + build).

This is the same authored-vs-captured asymmetry the demand_log/review_queue
split resolved: captured state belongs in a gitignored sidecar, never in the
tracked spine. So used prices now live HERE, in one gitignored file keyed by
card_id, and build_site overlays them at build time.

Contract:
  - refresh_used_prices.py WRITES the sidecar (one file, whole-file atomic).
  - build_site.py READS the sidecar and overlays each card's used_market onto
    the (static, tracked) card JSON before rendering. A card with no sidecar
    entry renders its honest "See used" fallback, exactly as a never-refreshed
    card does today.

The card JSON keeps `pricing.used_query` (AUTHORED: how to search for this
product's used market) but no longer carries the prices themselves (CAPTURED).

Shape:
    {
      "_description": "...",
      "as_of": "2026-06-25",
      "prices": {
        "sony-a7iv": {
          "source": "ebay",
          "bands": {"pre_owned": 697.0, "open_box": 1750.0, ...},
          "sample_size": 31,
          "price_updated_at": "2026-06-24T10:10:04.846349+00:00"
        },
        ...
      }
    }
"""
import json
import os
import tempfile
import time
from pathlib import Path

SIDECAR_PATH = Path(__file__).parent.parent / "data" / "used_prices.json"


def _empty():
    return {
        "_description": (
            "Captured used-market prices, refreshed on the box from eBay. "
            "OUTSIDE the tracked card spine (gitignored) — build_site overlays "
            "these onto static card JSON at build time. The card owns used_query "
            "(how to search); this owns what the search found."
        ),
        "as_of": time.strftime("%Y-%m-%d", time.gmtime()),
        "prices": {},
    }


def load(path=SIDECAR_PATH):
    """Return the sidecar dict, or a fresh empty one if absent.

    Tolerant of a missing file (first run / a box that has never refreshed),
    not of a corrupt one — a malformed used_prices.json is a real error the
    caller should see, mirroring skus_registry.load_registry discipline.
    """
    path = Path(path)
    if not path.exists():
        return _empty()
    return json.loads(path.read_text(encoding="utf-8"))


def get_used_market(card_id, path=SIDECAR_PATH):
    """Return the used_market block for one card_id, or None if not present."""
    return load(path).get("prices", {}).get(card_id)


def set_used_market(card_id, used_market, path=SIDECAR_PATH):
    """Write one card's used_market block into the sidecar (atomic whole-file).

    Read-modify-write of the single sidecar file. The refresh job calls this
    once per card it successfully prices; a gated card (too few survivors) is
    simply not written, so its absence keeps the honest fallback — identical to
    the old in-card behavior where used_market was left untouched.
    """
    path = Path(path)
    sidecar = load(path)
    sidecar.setdefault("prices", {})[card_id] = used_market
    sidecar["as_of"] = time.strftime("%Y-%m-%d", time.gmtime())
    _atomic_write(sidecar, path)
    return used_market


def overlay(card, path=SIDECAR_PATH):
    """Merge this card's sidecar used_market into card['pricing'] in place.

    The single overlay point build_site uses. If the sidecar has prices for
    this card_id, they populate card['pricing']['used_market'] before any
    renderer reads it; if not, the card is left as-is (no used_market → honest
    "See used" fallback). Returns the card for chaining.

    Non-destructive to everything else in pricing (used_query, amazon_asin,
    ebay_epid, affiliate_url all survive): only the used_market key is set.
    """
    card_id = card.get("card_id")
    if not card_id:
        return card
    um = get_used_market(card_id, path)
    if um is not None:
        card.setdefault("pricing", {})["used_market"] = um
    return card


def _atomic_write(sidecar, path=SIDECAR_PATH):
    """Atomic whole-file write (temp in same dir + os.replace).

    A reader (a concurrent build) never observes a half-written sidecar. Same
    temp+replace dance skus_registry and review_queue use for their mutable maps.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".up-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(sidecar, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
