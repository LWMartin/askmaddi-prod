#!/usr/bin/env python3
"""
refresh_used_prices.py — populate pricing.used_market from the eBay Browse API.

Runs on the VPS (where eBay creds live) against the local gateway:

    python3 tools/refresh_used_prices.py --cards-dir data/cards/ \
        --gateway http://127.0.0.1:5001

For each card it queries /ebay/search, filters listings down to genuine
used-market offers for THE product (not caps/straps/parts), trims outliers,
and writes condition-bucketed price bands. build_site.py's used_cta() then
renders "from $X used" from min(bands) — so every number written here must be
a real, clickable, payable price.

Doctrine (precision over recall):
  - ACTIVE asking prices only — the Browse API does not return sold comps,
    so sold_last_90d is never fabricated.
  - Bands are written only when >= MIN_SAMPLE survivors remain after
    filtering; otherwise used_market is left untouched and the CTA keeps
    its honest "See used" fallback.
  - Junk floor / kit ceiling trims relative to the survivor median keep
    "from $X" from pointing at a lens cap or a 4-lens bundle.

Offline / test mode: --from-json DIR reads {card_id}.json fixtures shaped
like the gateway response ({"items": [...]}) instead of calling HTTP.
"""

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

MIN_SAMPLE = 3          # minimum surviving listings before bands are asserted
JUNK_FLOOR = 0.4        # drop price < 0.4 x median (accessories, parts slipped through)
KIT_CEILING = 2.5       # drop price > 2.5 x median (multi-item bundles)
FETCH_LIMIT = 50

# Query tokens that must NOT be required to appear in listing titles.
# NOTE: variant discriminators (e.g. "carbon") are deliberately ABSENT —
# a variant token in the query must bind, or wrong-variant listings
# contaminate the bands (caught live 2026-06-10: aluminum tripods passed
# the carbon query's gate while carbon/fiber sat in this set).
TOKEN_STOPWORDS = {"body", "the", "for", "with", "and", "kit", "only"}

# A listing whose title contains any of these is not the product itself.
# Canonical vocabulary lives in gateway/rebind_firewall.py (single home,
# 2026-07-17 consolidation) — a strict superset of the historical list
# here, adding 'empty box' et al. (the 2026-07-16 poison-rebind class,
# which pollutes used-price bands exactly as it poisons rebinds).
# Matching stays SUBSTRING (this gate's historical semantics): aggressive
# is correct for price-band precision — a dropped legit listing costs one
# sample; an admitted accessory poisons the median. The firewall applies
# word-boundary matching to the same words for its own, stricter purpose.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'gateway'))
from rebind_firewall import JUNK_PHRASES, JUNK_WORDS  # noqa: E402

TITLE_BLACKLIST = list(JUNK_PHRASES) + list(JUNK_WORDS)

# eBay condition strings -> band slug. None = excluded from used bands.
def condition_slug(cond):
    c = (cond or "").strip().lower()
    if not c:
        return None
    if "parts" in c or "not working" in c:
        return None
    if "open box" in c:
        return "open_box"
    if "refurb" in c:
        return "refurbished"
    if "excellent" in c:
        return "excellent"
    if "very good" in c:
        return "very_good"
    if "good" in c:
        return "good"
    if "acceptable" in c:
        return "acceptable"
    if "pre-owned" in c or "pre owned" in c or c == "used":
        return "pre_owned"
    if "new" in c:
        return None  # new-market listing; this job prices the used market
    return "pre_owned"  # unknown-but-not-new: bucket conservatively


def significant_tokens(query):
    toks = [t.lower() for t in re.split(r"[\s/|]+", query) if len(t) >= 2]
    return [t for t in toks if t not in TOKEN_STOPWORDS]


def listing_matches(title, tokens):
    t = title.lower()
    t_nospace = re.sub(r"[\s\-]+", "", t)
    if any(bad in t for bad in TITLE_BLACKLIST):
        return False
    return all(tok in t or re.sub(r"[\s\-]+", "", tok) in t_nospace for tok in tokens)


def parse_price(item):
    try:
        v = float(item.get("price") or 0)
    except (TypeError, ValueError):
        return None
    cur = (item.get("currency") or "USD").upper()
    if cur != "USD" or v <= 0:
        return None
    return v


def compute_bands(items, query):
    """Filter gateway items -> (bands dict, sample_size). Empty dict if gated."""
    tokens = significant_tokens(query)
    survivors = []  # (slug, price)
    for it in items:
        slug = condition_slug(it.get("condition"))
        if slug is None:
            continue
        if not listing_matches(it.get("name") or "", tokens):
            continue
        price = parse_price(it)
        if price is None:
            continue
        survivors.append((slug, price))

    if len(survivors) < MIN_SAMPLE:
        return {}, len(survivors)

    med = median(p for _, p in survivors)
    trimmed = [(s, p) for s, p in survivors
               if JUNK_FLOOR * med <= p <= KIT_CEILING * med]
    if len(trimmed) < MIN_SAMPLE:
        return {}, len(trimmed)

    bands = {}
    for slug, price in trimmed:
        if slug not in bands or price < bands[slug]:
            bands[slug] = round(price, 2)
    return bands, len(trimmed)


def fetch_gateway(gateway, query):
    url = f"{gateway.rstrip('/')}/ebay/search?" + urllib.parse.urlencode(
        {"q": query, "limit": FETCH_LIMIT}
    )
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.load(resp)
    return data.get("items", []) or []


def refresh_card(path, items, dry_run=False):
    card = json.loads(path.read_text(encoding="utf-8"))
    pricing = card.setdefault("pricing", {})
    query = pricing.get("used_query") or card["identity"]["display_name"]

    bands, n = compute_bands(items, query)
    if not bands:
        print(f"  ~ {path.stem}: gated ({n} survivors < {MIN_SAMPLE}) — used_market untouched")
        return False

    used = pricing.setdefault("used_market", {})
    used.update({
        "source": "ebay",
        "bands": bands,
        "sample_size": n,
        "price_updated_at": datetime.now(timezone.utc).isoformat(),
    })
    lo = min(bands.values())
    print(f"  + {path.stem}: from ${int(lo)} used | {n} listings | bands={list(bands)}")
    if not dry_run:
        path.write_text(json.dumps(card, indent=2, ensure_ascii=False), encoding="utf-8")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cards-dir", default="data/cards/")
    ap.add_argument("--gateway", default="http://127.0.0.1:5001",
                    help="askmaddi gateway base URL (VPS local loopback)")
    ap.add_argument("--from-json", default=None,
                    help="dir of {card_id}.json gateway-shaped fixtures (offline/test)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cards = sorted(Path(args.cards_dir).glob("*.json"))
    if not cards:
        sys.exit(f"no cards found in {args.cards_dir}")

    changed = 0
    for path in cards:
        card = json.loads(path.read_text(encoding="utf-8"))
        query = (card.get("pricing", {}).get("used_query")
                 or card["identity"]["display_name"])
        if args.from_json:
            fx = Path(args.from_json) / f"{path.stem}.json"
            if not fx.exists():
                print(f"  ~ {path.stem}: no fixture — skipped")
                continue
            items = json.loads(fx.read_text(encoding="utf-8")).get("items", [])
        else:
            try:
                items = fetch_gateway(args.gateway, query)
            except Exception as e:
                print(f"  ! {path.stem}: gateway fetch failed ({type(e).__name__}: {e})")
                continue
        if refresh_card(path, items, dry_run=args.dry_run):
            changed += 1

    print(f"\nDone. {changed}/{len(cards)} card(s) updated"
          + (" (dry run — nothing written)" if args.dry_run else "")
          + ". Rebuild pages: python3 tools/build_site.py --cards-dir data/cards/ --output-dir browser/ --manifest")


if __name__ == "__main__":
    main()
