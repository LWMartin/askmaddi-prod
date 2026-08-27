"""
ebay_category_tap.py — the reusable, vertical-agnostic eBay-category demand intake.
===================================================================================
The FOURTH sibling in the proposals family (spec maddi-ebay-category-sourcing),
and the one that finally opens verticals the Adorama product feed cannot supply.

WHY THIS EXISTS. Measured 2026-08-27: the live 229k-row Adorama affiliate feed
carries ZERO flyable drones from ANY brand (DJI/Autel/Skydio/Parrot all absent) —
only the accessory tail (bags, cases, batteries). The US Chinese-drone squeeze
(NDAA / Countering CCP Drones Act) leads affiliate retailers to carve flyable
drones out of their feeds. So the aerial vertical's unblock is a new SUPPLY
SOURCE, not a feed-scope toggle. eBay's Browse API (gateway/ebay_api.py — already
our identity spine, EPN-tagged) carries drones in volume, new and used. Live-
confirmed same day: Mavic 3 Pro / Autel EVO Lite / Skydio X10D, ready-to-fly.

WHERE THIS SITS. Identical downstream to the other three intakes — it WRITES a
resolve-pass proposals artifact ([{slug, fork_n, vendor, model}], the shape
resolve_pass.load_proposals consumes) and stops. It does not resolve, enroll, or
publish. resolve_pass (eBay + Gemma) resolves; the human air-gap gate sharpens;
card_factory drips. No new pipeline — one new fuel door.

  comparator_mine_typed  (corpus-internal)   ─┐
  gear_release_adapter   (news herald)        ├─► proposals.json ─► resolve_pass ─► drip
  demand_surface_adapter (Adorama feed)       │
  ebay_category_tap      (eBay category) ← THIS┘

REUSABLE BY CONSTRUCTION (Lee's directive: "the process will be repeated for other
verticals"). This is NOT a drone module. Behaviour is driven by
data/ebay_source_verticals.json — one row per vertical (facet, eBay category id,
seed queries, condition scope). Drone is row 1; the real-estate 360 / virtual-tour
row is the next, and its acceptance test is that it opens WITHOUT touching this
file. Real estate is the demand thesis behind the sequence (aerial property shots,
interiors, walkthroughs, virtual tours).

OFFLINE-TESTABLE (resolve_pass discipline). The network + cross-repo screen are
INJECTED: run(..., search_fn=, classify_fn=, slug_fn=, known=) so the fan-out,
facet screen, slug, dedup and cap are unit-tested with fakes; the real eBay search
and the de-DJI demand_gate are proven on the box. classify_fn defaults to the
aggregator's demand_gate.classify when importable (so Lee's de-DJI + accessory
precision IS the screen); it degrades to a permissive pass off-box.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PROD = _HERE.parent                       # /opt/askmaddi-prod
DEFAULT_CONFIG = _PROD / "data" / "ebay_source_verticals.json"
DEFAULT_SKUS = _PROD / "data" / "skus.json"
DEFAULT_WORK_QUEUE = _PROD / "data" / "work_queue.json"
DEFAULT_PROPOSALS = Path("/var/lib/askmaddi-pipeline/proposals.json")

# The aggregator (phantom-ops) that holds the de-DJI demand_gate + slug tools.
# The nightly adapters already run with this on PYTHONPATH; we also probe the
# canonical box path so a direct/manual run resolves the screen too.
_AGG_PATHS = [
    os.environ.get("AGGREGATOR_PATH", ""),
    "/home/phantomops/phantom-ops/claude/workspace/aggregator-build",
    "/home/claude/phantom-ops/claude/workspace/aggregator-build",
]

# Drone-market brands (title-cased) for brand inference — eBay item summaries
# frequently return brand='' so we recover it from the title. Order matters only
# for multi-word names ("Holy Stone" before "Holy").
_BRANDS = [
    "DJI", "Autel", "Skydio", "Parrot", "BRINC", "Anzu", "Potensic",
    "Holy Stone", "Ruko", "HoverAir", "Hover", "Zero Zero", "Ryze", "Tello",
]

# Marketing / condition / bundle tail stripped from a listing title to recover a
# clean vendor+model (resolve mints from vendor+model; the human gate sharpens).
_NOISE = re.compile(
    r"\b(ready[- ]?to[- ]?fly|rtf|fly\s*more(\s*combo)?|combo|bundle|kit|"
    r"brand\s*new|new|open\s*box|used|pre[- ]?owned|refurbished|renewed|"
    r"excellent|mint|sealed|genuine|official|authentic|us\s*version|"
    r"with|w/|for|and|the|camera|drone|quadcopter|uav|aerial|4k|5\.?1k|6k|8k|"
    r"gps|hd|fpv)\b",
    re.IGNORECASE,
)
_PARENS = re.compile(r"[\(\[].*?[\)\]]")
_NONSLUG = re.compile(r"[^a-z0-9]+")

# Drone model-core extractor. The aggregator's mention_connector is tuned to
# CAMERA/lens model designators (a7iv, eos-r) and would sink every drone, so a
# vertical needs its own canonical-core matcher — the drone analogue of
# MODEL_PATTERNS. Matches "<brand?> <line> <number?> <variant?>" and yields the
# clean identity core, dropping the listing's marketing tail. Extend per new
# line as the market moves (same maintenance surface as the demand_gate patterns).
_DRONE_MODEL = re.compile(
    r"\b(?:dji|autel|skydio|parrot|potensic|ruko|hoverair|holy\s*stone|anzu|brinc)?\s*"
    r"(mavic|air|mini|neo|avata|inspire|flip|phantom|evo(?:\s*lite|\s*nano|\s*max|\s*ii)?|"
    r"anafi|x10|x2|atom|hs\d+|tello)\s*"
    r"(\d+)?\s*"
    r"(pro|classic|plus|se|cine|lite|nano|max|ii|iii|s|t|enterprise|creator|adv(?:anced)?)?\b",
    re.IGNORECASE,
)

# Local accessory / parts sink for the eBay path. The shared demand_gate
# deliberately defers battery/controller/parts to covered_precision (the Qwen3
# screen), which is NOT in this tap's loop — so a listing whose HEAD NOUN is a
# part must be dropped here. Generic across verticals (a "replacement body only"
# / "batteries" / "for <model>" listing is an accessory in any vertical).
_LOCAL_ACCESSORY = re.compile(
    r"\b(batter(?:y|ies)|propell?ers?|\bprops?\b|controller|remote\s*control(?:ler)?|"
    r"charg(?:er|ing)|charging\s*hub|\bhub\b|\bcase\b|\bbag\b|backpack|foam|"
    r"replacement|\bparts?\b|\bbody\s*only\b|\bshell\b|cover|guard|\bstrap\b|"
    r"gimbal\s*(?:cover|guard|repair)|nd\s*filter|\bfilter\b|lens\s*hood|"
    r"landing\s*gear|antenna|sd\s*card|memory\s*card|screen\s*protector|"
    r"decal|skin\s*sticker|\bmount\b|adapter|cable|\bfor\s+dji\b)\b",
    re.IGNORECASE,
)


def _import_classify():
    """The de-DJI demand_gate.classify if the aggregator is importable, else None."""
    for p in _AGG_PATHS:
        if p and p not in sys.path and Path(p).exists():
            sys.path.insert(0, p)
    try:
        from ingest.demand_gate import classify  # type: ignore
        return classify
    except Exception:
        return None


def _guess_brand(title: str) -> str:
    low = title.lower()
    for b in _BRANDS:
        if re.search(r"\b" + re.escape(b.lower()) + r"\b", low):
            return b
    # fall back to the first title token (eBay titles usually lead with brand)
    tok = title.strip().split()
    return tok[0] if tok else ""


def _clean_model(title: str) -> str:
    """Recover a clean 'Brand Model' string from a noisy listing title."""
    t = _PARENS.sub(" ", title)
    t = _NOISE.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip(" -,–|")
    return t


def default_slug(brand: str, title: str) -> str:
    """Drone-aware brand+model slug in the card convention (dji-mavic-3-pro).

    Prefers the canonical drone model-core (_DRONE_MODEL) so a noisy listing
    title ("DJI Neo 2 * USA In Stock * 2-4 Shipping") collapses to the identity
    (dji-neo-2) rather than carrying the marketing tail into the slug. Falls back
    to a trimmed slugify when no drone core is found. resolve_proposal mints from
    vendor+model regardless; the review gate sharpens either way."""
    m = _DRONE_MODEL.search(title)
    if m:
        line, num, variant = m.group(1), m.group(2), m.group(3)
        core = " ".join(x for x in (brand, line, num, variant) if x)
    else:
        core = f"{brand} {_clean_model(title)}".strip()
    slug = _NONSLUG.sub("-", core.lower()).strip("-")
    dedup = []
    for w in slug.split("-"):
        if w and not (dedup and dedup[-1] == w):     # collapse doubled tokens
            dedup.append(w)
    return "-".join(dedup)[:60].strip("-")


def known_slugs(skus_path=DEFAULT_SKUS, work_queue_path=DEFAULT_WORK_QUEUE) -> set:
    """Every slug already BUILT (skus.json) or QUEUED (work_queue.json) — the
    pre-filter dedup set. Absent files contribute nothing (safe degradation:
    worst case a wasted resolve, which resolve_pass's own dedup still catches)."""
    slugs: set = set()
    p = Path(skus_path)
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        registry = data.get("skus", data) if isinstance(data, dict) else {}
        if isinstance(registry, dict):
            slugs |= set(registry.keys())
    wq = Path(work_queue_path)
    if wq.exists():
        data = json.loads(wq.read_text(encoding="utf-8"))
        slugs |= set((data.get("queue") or {}).keys())
    return slugs


def run(verticals, *, search_fn, classify_fn=None, slug_fn=None,
        known=frozenset(), per_query_limit=25, cap=None):
    """Fan seed queries over eBay per vertical → facet-screen → slug → dedup →
    proposals. Pure orchestration; all I/O is injected.

    verticals : list of config rows (see data/ebay_source_verticals.json).
    search_fn : (query, limit, category_ids, condition_ids) -> [ {item_id,title,
                brand,price,condition,epid}, ... ]  (ebay_api.search_candidates).
    classify_fn : (row_dict) -> {"category": ...}   (demand_gate.classify). When
                None, no facet screen is applied (permissive — off-box tests).
    slug_fn : (brand, title) -> slug. Defaults to default_slug.
    known : slugs already built/queued (dedup).
    Returns list of proposals [{slug, fork_n, vendor, model, source}], deduped.
    """
    slug_fn = slug_fn or default_slug
    seen = set(known)
    out = []
    for v in verticals:
        if not v.get("enabled", True):
            continue
        facet = v.get("facet", "")
        gate_cat = v.get("gate_category", facet)
        cat_id = v.get("ebay_category_id") or None
        cond = v.get("condition_ids")            # None => new + used
        for q in v.get("seed_queries", []):
            try:
                rows = search_fn(q, limit=per_query_limit,
                                 category_ids=cat_id, condition_ids=cond)
            except Exception as e:
                print(f"  [warn] search '{q}' failed: {e}", file=sys.stderr)
                continue
            for r in rows:
                title = (r.get("title") or "").strip()
                if not title:
                    continue
                brand = (r.get("brand") or "").strip() or _guess_brand(title)
                if _LOCAL_ACCESSORY.search(title):
                    continue                     # part/battery/case — covered_precision's
                                                 # job downstream, but not in this loop
                if classify_fn is not None:
                    verdict = classify_fn({"brand": brand, "model": title})
                    if (verdict or {}).get("category") != gate_cat:
                        continue                 # accessory / off-facet — drop
                slug = slug_fn(brand, title)
                if not slug or "-" not in slug:  # need brand + ≥1 distinguisher
                    continue
                if slug in seen:
                    continue
                seen.add(slug)
                out.append({
                    "slug": slug, "fork_n": 0, "vendor": brand,
                    "model": _clean_model(title), "source": f"ebay:{facet}",
                })
                if cap and len(out) >= cap:
                    return out
    return out


def merge_into(proposals_path, new) -> tuple:
    """Union `new` into resolve-pass proposals.json BY SLUG (append absent, keep
    existing). Mirrors demand_surface_adapter.merge_into. Returns (added, total)."""
    p = Path(proposals_path)
    existing = []
    if p.exists():
        existing = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(existing, list):
            raise ValueError(f"{p} is not a JSON list of proposals")
    have = {e.get("slug") for e in existing if isinstance(e, dict)}
    added = 0
    for prop in new:
        if prop["slug"] not in have:
            existing.append(prop)
            have.add(prop["slug"])
            added += 1
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    return added, len(existing)


def load_config(path=DEFAULT_CONFIG):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data.get("verticals", data if isinstance(data, list) else [])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--into", default=None,
                    help="merge proposals into this resolve-pass proposals.json")
    ap.add_argument("--out", default=None,
                    help="write proposals to this file (dry-run artifact for review)")
    ap.add_argument("--limit", type=int, default=25, help="per-query eBay limit")
    ap.add_argument("--cap", type=int, default=None, help="max proposals emitted")
    ap.add_argument("--no-screen", action="store_true",
                    help="skip the demand_gate facet screen (debug)")
    args = ap.parse_args(argv)

    try:
        import ebay_api  # noqa: E402  (box: gateway on sys.path)
    except Exception:
        sys.path.insert(0, str(_PROD / "gateway"))
        import ebay_api  # type: ignore

    classify_fn = None if args.no_screen else _import_classify()
    if not args.no_screen and classify_fn is None:
        print("  [warn] demand_gate.classify not importable — running WITHOUT the "
              "facet screen (accessory tail will not be dropped)", file=sys.stderr)

    verticals = load_config(args.config)
    known = known_slugs()
    proposals = run(verticals, search_fn=ebay_api.search_candidates,
                    classify_fn=classify_fn, known=known,
                    per_query_limit=args.limit, cap=args.cap)

    print(f"eBay category tap: {len(proposals)} proposal(s) "
          f"({len(known)} slugs already known, screen="
          f"{'on' if classify_fn else 'OFF'})")
    for p in proposals[:40]:
        print(f"  {p['slug']:40s} {p['vendor']} | {p['model'][:40]}")

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(proposals, indent=2) + "\n",
                                  encoding="utf-8")
        print(f"wrote dry-run artifact -> {args.out}")
    if args.into:
        added, total = merge_into(args.into, proposals)
        print(f"merged into {args.into}: +{added} new (total {total})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
