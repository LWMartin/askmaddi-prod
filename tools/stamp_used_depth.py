#!/usr/bin/env python3
"""stamp_used_depth.py — stamp used-market DEPTH onto the demand SURFACE so the
dual-signal demand gate can open the used/vintage lane.

WHY. `demand_surface_adapter._clears_demand_gate` (phantom-ops) admits a covered
candidate if it clears EITHER agnostic signal — mention/recency demand OR
used-market DEPTH (count of genuine active used listings >= the vertical's
`used_depth_floor`). The mention half has run since day one; the used-depth half
was DORMANT because nothing wrote `row["used_depth"]`. This is that producer.

WHERE THE COUNT COMES FROM. `refresh_used_prices.compute_bands()` already filters
gateway `/ebay/search` listings down to genuine used matches (used-condition +
title-match + parseable price, then junk-floor/kit-ceiling trim) and returns that
survivor COUNT as its second value. We reuse it verbatim — same filter, same
honest floor the price bands are built on — so depth and price agree by
construction (no second, drifting notion of "a genuine used listing").

WHICH ROWS. Only UNCARDED + UNMENTIONED covered rows: a mentioned row already
clears the gate, a carded row is already built. This is exactly the set the used
lane would NEWLY admit, and it minimizes eBay calls.

COST / SAFETY. Every target row is stamped from a TTL key-cache for free; only
cache-missing/stale rows spend an eBay call, capped at `--limit` per run. So a
large backlog fills over several polite nights, then steady-state is near-zero
calls. Failure is NON-FATAL by construction: a row we don't reach keeps no
`used_depth` (absent -> the gate reads 0 -> the used lane simply stays shut for
it). The producer never NARROWS what mention already admitted.

Usage:
    python3 tools/stamp_used_depth.py \
        --surface /home/phantomops/feed-snapshots/adorama/demand-gated.json \
        --gateway http://127.0.0.1:5001 --limit 150

    # Inspect without writing (manual validation):
    python3 tools/stamp_used_depth.py --surface ... --dry-run

    # Offline / test: read {key}.json fixtures shaped like /ebay/search
    # ({"items":[...]}) from DIR instead of the network:
    python3 tools/stamp_used_depth.py --surface ... --from-json fixtures/depth/
"""
import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Reuse the SAME filter/counter the price job trusts — do not re-hand-roll it.
from refresh_used_prices import compute_bands, fetch_gateway  # noqa: E402

CACHE_NAME = "used-depth-cache.json"   # sits beside the surface it annotates
DEFAULT_TTL_DAYS = 30
DEFAULT_LIMIT = 150


# Generic gear descriptors + color/noise words. Feed `model` fields are verbose
# marketing titles ("Sony Cyber-shot DSC-RX100 V Digital Camera, Black"); because
# `listing_matches` requires EVERY significant query token, leaving these in
# over-constrains the search and reads a deep used market as depth 0 (measured on
# the RX100 V during 2026-08-19 live validation). We keep brand words, any token
# bearing a digit (model numbers), and distinctive words; we drop only these.
_GENERIC_TOKENS = frozenset({
    "digital", "camera", "cameras", "mirrorless", "dslr", "slr", "body",
    "lens", "kit", "bundle", "set", "system", "full", "hd", "uhd", "4k", "8k",
    "black", "silver", "white", "gray", "grey", "graphite", "titanium",
    "gold", "blue", "red", "green", "for", "with", "new", "the", "and",
    "rechargeable", "professional", "pro",
})


def build_query(row) -> str:
    """The eBay search string for a candidate. Two shaping steps:
    (1) feed rows frequently double the brand into the model ('Hasselblad
        Hasselblad High Capacity ...') — don't repeat it;
    (2) drop generic descriptor/color tokens so `listing_matches` isn't
        over-constrained, while ALWAYS keeping brand words and model-number
        tokens (any token with a digit). Never returns empty — if trimming would
        leave nothing, fall back to the untrimmed brand+model."""
    brand = (row.get("brand") or "").strip()
    model = (row.get("model") or "").strip()
    if not model:
        return brand
    raw = model if (brand and model.lower().startswith(brand.lower())) \
        else f"{brand} {model}".strip()

    brand_toks = {t.lower() for t in brand.split()}
    kept = []
    for tok in raw.split():
        core = tok.strip(",.;:()[]").lower()
        if not core:
            continue
        if (core in brand_toks or any(c.isdigit() for c in core)
                or core not in _GENERIC_TOKENS):
            kept.append(tok.strip(",.;:()[]"))
    return " ".join(kept) if kept else raw


def is_target(row) -> bool:
    """Only rows the used lane could NEWLY admit: not already carded (built) and
    not already mentioned (mention already clears the gate)."""
    return not row.get("carded") and not row.get("mentioned")


def is_fresh(entry, now, ttl_days) -> bool:
    try:
        checked = datetime.fromisoformat(entry["checked_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return (now - checked) <= timedelta(days=ttl_days)


def load_json(path, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def atomic_write(path, obj):
    """Write JSON to `path` via a same-dir temp + os.replace, preserving the
    destination's mode if it already exists (the surface is a live-read,
    phantomops-owned artifact — don't clobber its perms; see the /opt 600-drop
    incident)."""
    path = Path(path)
    mode = path.stat().st_mode & 0o777 if path.exists() else None
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def fetch_depth(row, gateway, from_json):
    """Return the genuine-used-listing count for a row, reusing compute_bands'
    survivor count (its 2nd return value). `from_json` DIR reads a per-key fixture
    ({"items":[...]}) instead of hitting the gateway."""
    query = build_query(row)
    if from_json:
        fx = Path(from_json) / f"{row.get('key')}.json"
        items = load_json(fx, {}).get("items", []) if fx.exists() else []
    else:
        items = fetch_gateway(gateway, query)
    _bands, depth = compute_bands(items, query)
    return depth, query


def run(surface_path, gateway, limit=DEFAULT_LIMIT, ttl_days=DEFAULT_TTL_DAYS,
        from_json=None, dry_run=False):
    """Stamp `used_depth` onto the surface's target rows. Returns a stats dict."""
    surface = load_json(surface_path, None)
    if surface is None:
        raise SystemExit(f"stamp_used_depth: no surface at {surface_path}")
    covered = surface.get("covered", [])

    cache_path = Path(surface_path).parent / CACHE_NAME
    cache = load_json(cache_path, {})
    now = datetime.now(timezone.utc)

    targets = [r for r in covered if is_target(r)]
    to_fetch = []
    stats = {"covered": len(covered), "targets": len(targets),
             "from_cache": 0, "fetched": 0, "deferred": 0,
             "cleared_default_floor": 0}

    # Pass 1 — stamp every fresh-cached target for free.
    for row in targets:
        entry = cache.get(row.get("key"))
        if entry and is_fresh(entry, now, ttl_days):
            row["used_depth"] = entry["depth"]
            stats["from_cache"] += 1
        else:
            to_fetch.append(row)

    # Pass 2 — spend up to `limit` eBay calls on cache misses/stale.
    for row in to_fetch[:limit]:
        depth, query = fetch_depth(row, gateway, from_json)
        row["used_depth"] = depth
        cache[row.get("key")] = {"depth": depth,
                                 "checked_at": now.isoformat(),
                                 "query": query}
        stats["fetched"] += 1

    stats["deferred"] = max(0, len(to_fetch) - limit)
    stats["cleared_default_floor"] = sum(
        1 for r in targets if (r.get("used_depth") or 0) >= 3)

    if not dry_run:
        atomic_write(surface_path, surface)
        atomic_write(cache_path, cache)

    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--surface", required=True,
                    help="path to demand-gated.json (the FEED_STORE_ROOT surface)")
    ap.add_argument("--gateway", default="http://127.0.0.1:5001",
                    help="askmaddi gateway base (proxies /ebay/search)")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help="max eBay calls (cache misses) per run")
    ap.add_argument("--ttl-days", type=int, default=DEFAULT_TTL_DAYS,
                    help="cache freshness window")
    ap.add_argument("--from-json", default=None,
                    help="read {key}.json fixtures from DIR instead of network")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute + report, write nothing")
    args = ap.parse_args()

    stats = run(args.surface, args.gateway, limit=args.limit,
                ttl_days=args.ttl_days, from_json=args.from_json,
                dry_run=args.dry_run)

    tag = "[dry-run] " if args.dry_run else ""
    print(f"{tag}stamp_used_depth: {stats['targets']} target rows "
          f"({stats['covered']} covered) | "
          f"{stats['from_cache']} from-cache + {stats['fetched']} fetched, "
          f"{stats['deferred']} deferred to next run")
    print(f"{tag}  {stats['cleared_default_floor']} clear the default "
          f"used_depth_floor (>=3) -> newly admissible to the used lane")


if __name__ == "__main__":
    main()
