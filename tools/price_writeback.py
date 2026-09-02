#!/usr/bin/env python3
"""
price_writeback — apply the reviewed Adorama New-price backfill onto card JSONs.

Consumes the review sheet produced by phantom-ops `ingest.price_backfill`
(price-backfill-latest.json) and writes the New/Adorama rung into each matched
card's `pricing` block:

    pricing.current_new_usd    <- new.price_usd
    pricing.current_new_url    <- new.affiliate_url   (feed product_url, prf.hn-wrapped)
    pricing.affiliate_url      <- new.affiliate_url    (what build_site.new_cta honours)
    pricing.current_new_source <- "adorama_feed"

USED IS NOT TOUCHED. Cards already carry a richer eBay `pricing.used_market`
(bands + sample_size + sold_last_90d); the spine's single price_seen would be a
downgrade, so this tool writes the New rung only.

    python3 tools/price_writeback.py <sheet.json> --cards-dir data/cards        # dry run
    python3 tools/price_writeback.py <sheet.json> --cards-dir data/cards --apply

Discipline:
  - Dry run by default: prints the planned change table and the flagged rows; writes
    nothing. --apply performs atomic per-card writes (tmp + os.replace), matching the
    card serialization (json indent=2, ensure_ascii=False, no trailing newline) so the
    diff is the price lines only.
  - Rows the sheet marks needs_eyes (ambiguous / gtin_agree=False) are HELD unless
    --include-flagged — they are the craft-seat review, not an auto-apply.
  - A proposal with no live card, or no New match, is skipped (its used rung already
    renders; the New CTA stays "Check price at Adorama", which is honest).
  - Prints the changed slugs (--changed-out writes them one-per-line) so the caller can
    rebuild exactly those cards + the manifest.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


CARD_SOURCE = "adorama_feed"


def _write_card_atomic(path: Path, card: dict) -> None:
    """Serialize exactly as the cards are stored (indent=2, no trailing newline)
    and replace atomically so a crash never leaves a half-written card."""
    text = json.dumps(card, indent=2, ensure_ascii=False)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _url_host(url: str) -> str:
    if not url:
        return ""
    s = url.split("//", 1)[-1]
    return s.split("/", 1)[0]


def plan(sheet: dict, cards_dir: Path, include_flagged: bool = False, exclude=None):
    """Return (changes, held, skipped) without touching disk.
    changes/held rows: {slug, old, new, matched_by, host, gtin_agree, ambiguous}.

    `exclude` is a set of slugs held out by the craft seat (e.g. a contaminated
    spine identity that made an MPN match land on the wrong product) — they are
    reported as held with reason 'excluded', never applied."""
    exclude = set(exclude or ())
    changes, held, skipped = [], [], []
    for p in sheet.get("proposals", []):
        new = p.get("new")
        if not new or not p.get("has_card"):
            continue
        card_path = cards_dir / f"{p['slug']}.json"
        if not card_path.exists():
            skipped.append({"slug": p["slug"], "reason": "card file missing"})
            continue
        pricing = json.loads(card_path.read_text(encoding="utf-8")).get("pricing", {}) or {}
        row = {
            "slug": p["slug"],
            "old": pricing.get("current_new_usd") or 0,
            "new": round(float(new["price_usd"])),
            "matched_by": new.get("matched_by"),
            "host": _url_host(new.get("affiliate_url") or ""),
            "gtin_agree": new.get("gtin_agree"),
            "ambiguous": new.get("ambiguous"),
            "url": new.get("affiliate_url") or "",
        }
        if p["slug"] in exclude:
            row["excluded"] = True
            held.append(row)
        elif p.get("needs_eyes") and not include_flagged:
            held.append(row)
        else:
            changes.append(row)
    return changes, held, skipped


def apply_changes(changes, cards_dir: Path) -> list:
    applied = []
    for row in changes:
        card_path = cards_dir / f"{row['slug']}.json"
        card = json.loads(card_path.read_text(encoding="utf-8"))
        pricing = card.setdefault("pricing", {})
        pricing["current_new_usd"] = row["new"]
        pricing["current_new_url"] = row["url"]
        pricing["affiliate_url"] = row["url"]
        pricing["current_new_source"] = CARD_SOURCE
        _write_card_atomic(card_path, card)
        applied.append(row["slug"])
    return applied


def patch_manifest(manifest_path: Path, changes) -> dict:
    """Surgically set new_price/new_url on the changed slugs' teaser entries in an
    EXISTING cards-manifest.json — never a full regen (the committed manifest is
    deliberately behind data/cards; regenerating would sweep in unrelated drift and
    revert cron-patched entries). Only the two New fields move; everything else on
    each entry, and every other entry, is left byte-for-byte. Matches the manifest
    serialization (indent=2, ensure_ascii=False, trailing newline)."""
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_slug = {r["slug"]: r for r in changes}
    entries = {c.get("card_id"): c for c in doc.get("cards", [])}
    patched, absent = [], []
    for slug, row in by_slug.items():
        entry = entries.get(slug)
        if not entry:
            absent.append(slug)
            continue
        pricing = entry.setdefault("pricing", {})
        pricing["new_price"] = row["new"]
        pricing["new_url"] = row["url"]
        patched.append(slug)
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    tmp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, manifest_path)
    return {"patched": patched, "absent": absent}


def _print_table(title, rows):
    if not rows:
        return
    print(f"\n{title} ({len(rows)}):")
    print(f"  {'slug':32}  {'old':>6}  {'new':>7}  {'by':4}  host")
    for r in rows:
        flag = ""
        if r.get("excluded"):
            flag += " ⛔excluded"
        if r.get("ambiguous"):
            flag += " ⚠amb"
        if r.get("gtin_agree") is False:
            flag += " ⚠gtin"
        print(f"  {r['slug'][:32]:32}  {int(r['old']):>6}  {'$'+str(r['new']):>7}  "
              f"{(r['matched_by'] or ''):4}  {r['host']}{flag}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Apply the Adorama New-price backfill onto card JSONs.")
    ap.add_argument("sheet", help="price-backfill-latest.json")
    ap.add_argument("--cards-dir", default="data/cards")
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    ap.add_argument("--include-flagged", action="store_true",
                    help="also apply needs_eyes rows (default: hold them for review)")
    ap.add_argument("--exclude", default="",
                    help="comma-separated slugs to hold (e.g. contaminated spine identity)")
    ap.add_argument("--changed-out", default=None, help="write changed slugs, one per line")
    ap.add_argument("--manifest", default=None,
                    help="cards-manifest.json to surgically patch (new_price/new_url on changed slugs only)")
    args = ap.parse_args(argv)

    sheet = json.loads(Path(args.sheet).read_text(encoding="utf-8"))
    cards_dir = Path(args.cards_dir)
    exclude = {s.strip() for s in args.exclude.split(",") if s.strip()}
    changes, held, skipped = plan(sheet, cards_dir, include_flagged=args.include_flagged,
                                  exclude=exclude)

    _print_table("New price — will apply", changes)
    _print_table("New price — HELD for review (needs eyes)", held)
    if skipped:
        print(f"\nskipped ({len(skipped)}): " + ", ".join(f"{s['slug']}({s['reason']})" for s in skipped))

    print(f"\nsummary: {len(changes)} to apply · {len(held)} held · {len(skipped)} skipped")
    if not args.apply:
        print("DRY RUN — nothing written. Re-run with --apply to write, then rebuild those cards + manifest.")
        return 0

    applied = apply_changes(changes, cards_dir)
    print(f"APPLIED {len(applied)} cards.")
    if args.manifest:
        res = patch_manifest(Path(args.manifest), changes)
        print(f"manifest patched: {len(res['patched'])} teaser entries"
              + (f" · {len(res['absent'])} changed slugs not on grid: {', '.join(res['absent'])}"
                 if res['absent'] else ""))
    if args.changed_out:
        Path(args.changed_out).write_text("\n".join(applied) + "\n", encoding="utf-8")
        print(f"  changed slugs -> {args.changed_out}")
    else:
        print("  changed slugs: " + " ".join(applied))
    return 0


if __name__ == "__main__":
    sys.exit(main())
