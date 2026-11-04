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


# ─── Withdrawal of stale feed prices (2026-11-04) ─────────────────────────────
# plan() only ever ADDS or MOVES a price, so a product that went out of stock or
# left the feed kept its last price on the card indefinitely — a "$N new" claim
# (and, once emitted, an InStock Offer) that was no longer true. Withdrawal
# clears ONLY current_new_usd on cards whose price came from the feed; the
# exact-product affiliate link and current_new_source stay, so the CTA reads
# "Check price at Adorama" on the product page, and the very next night the
# item reappears in stock, plan() restores the price with no special path.
# A card changes only when its stock state changes — never a nightly heartbeat.
WITHDRAW_MAX_FRACTION = 0.20   # above this share in ONE run = a feed fault, not the market


def plan_withdrawals(sheet: dict, cards_dir: Path, exclude=None,
                     max_fraction: float = WITHDRAW_MAX_FRACTION):
    """Return (withdrawals, withheld_reason) without touching disk.

    A withdrawal is a sheet proposal with has_card and new=None (no in-stock New
    match in today's feed) for a card that currently shows a feed-sourced price.
    If withdrawals exceed `max_fraction` of the feed-priced cards, ALL are
    withheld and a reason is returned: a truncated or failed feed download looks
    exactly like the whole catalog going out of stock, and wiping prices on a bad
    download is worse than one more day of a possibly-stale price."""
    exclude = set(exclude or ())
    feed_priced = 0
    for path in cards_dir.glob("*.json"):
        try:
            p = (json.loads(path.read_text(encoding="utf-8")) or {}).get("pricing") or {}
        except ValueError:
            continue
        if p.get("current_new_source") == CARD_SOURCE and p.get("current_new_usd"):
            feed_priced += 1

    withdrawals, seen = [], set()
    for prop in sheet.get("proposals", []):
        slug = prop.get("slug")
        if prop.get("new") or not prop.get("has_card") or slug in exclude or slug in seen:
            continue
        card_path = cards_dir / f"{slug}.json"
        if not card_path.exists():
            continue
        pricing = json.loads(card_path.read_text(encoding="utf-8")).get("pricing") or {}
        if pricing.get("current_new_source") != CARD_SOURCE or not pricing.get("current_new_usd"):
            continue  # not a feed price (hand-set, or already withdrawn) — not ours to clear
        seen.add(slug)
        withdrawals.append({"slug": slug, "old": pricing["current_new_usd"],
                            "url": pricing.get("affiliate_url") or pricing.get("current_new_url")})

    if withdrawals and feed_priced and len(withdrawals) / feed_priced > max_fraction:
        return [], (f"{len(withdrawals)} of {feed_priced} feed-priced cards would lose their "
                    f"price (> {max_fraction:.0%}) — looks like a feed fault; ALL withheld")
    return withdrawals, None


def apply_withdrawals(withdrawals, cards_dir: Path) -> list:
    done = []
    for row in withdrawals:
        card_path = cards_dir / f"{row['slug']}.json"
        card = json.loads(card_path.read_text(encoding="utf-8"))
        card.setdefault("pricing", {})["current_new_usd"] = None
        _write_card_atomic(card_path, card)
        done.append(row["slug"])
    return done


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
    ap.add_argument("--no-withdraw", action="store_true",
                    help="do not clear feed prices that have no in-stock match today")
    ap.add_argument("--max-withdraw-fraction", type=float, default=WITHDRAW_MAX_FRACTION,
                    help="withhold ALL withdrawals if more than this share of feed-priced "
                         f"cards would lose their price in one run (default {WITHDRAW_MAX_FRACTION})")
    args = ap.parse_args(argv)

    sheet = json.loads(Path(args.sheet).read_text(encoding="utf-8"))
    cards_dir = Path(args.cards_dir)
    exclude = {s.strip() for s in args.exclude.split(",") if s.strip()}
    changes, held, skipped = plan(sheet, cards_dir, include_flagged=args.include_flagged,
                                  exclude=exclude)

    withdrawals, withheld = [], None
    if not args.no_withdraw:
        priced_now = {r["slug"] for r in changes} | {r["slug"] for r in held}
        withdrawals, withheld = plan_withdrawals(sheet, cards_dir, exclude=exclude | priced_now,
                                                 max_fraction=args.max_withdraw_fraction)

    _print_table("New price — will apply", changes)
    _print_table("New price — HELD for review (needs eyes)", held)
    if withdrawals:
        print(f"\nNew price — WITHDRAW (no in-stock feed match today) ({len(withdrawals)}):")
        for w in withdrawals:
            print(f"  {w['slug'][:32]:32}  ${int(w['old'])} -> (no price)")
    if withheld:
        print(f"\nWARN withdrawals WITHHELD: {withheld}")
    if skipped:
        print(f"\nskipped ({len(skipped)}): " + ", ".join(f"{s['slug']}({s['reason']})" for s in skipped))

    print(f"\nsummary: {len(changes)} to apply · {len(withdrawals)} to withdraw · "
          f"{len(held)} held · {len(skipped)} skipped")
    if not args.apply:
        print("DRY RUN — nothing written. Re-run with --apply to write, then rebuild those cards + manifest.")
        return 0

    applied = apply_changes(changes, cards_dir)
    withdrawn = apply_withdrawals(withdrawals, cards_dir)
    print(f"APPLIED {len(applied)} cards · WITHDREW {len(withdrawn)}.")
    applied = applied + withdrawn
    if args.manifest:
        res = patch_manifest(Path(args.manifest), changes
                             + [{"slug": w["slug"], "new": None, "url": w["url"]} for w in withdrawals])
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
