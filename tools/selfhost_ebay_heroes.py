#!/usr/bin/env python3
"""Self-host the eBay fallback hero images — the de-eBay pass for gear Wikipedia
and Adorama do not cover (used/vintage lenses, tripods, film bodies, discontinued
gear that lives only on eBay).

For every card that still fronts an i.ebayimg.com listing thumbnail AND has no
clean Wikipedia/Commons hero, we fetch that image once into
browser/images/heroes/<card_id>.jpg and record it in data/selfhost_images.json.
build_site.apply_selfhost_registry then serves it from our own origin, so the
i.ebayimg.com host never appears in the Product image / og:image / the page, and
the hero stops breaking when the underlying eBay listing ends (their URLs churn).

This is a SECOND tier below the clean hero registry: a card that later gets a
Wikipedia/Commons photo (hero_images.json) auto-upgrades — the clean hero wins in
apply order, so the self-host copy is a floor, never a ceiling.

Idempotent: a card already downloaded is skipped unless --force. Polite: a fixed
delay between fetches. Preview with --dry-run.

    python tools/selfhost_ebay_heroes.py \
        --cards-dir data/cards --images-dir browser/images/heroes \
        --registry data/selfhost_images.json --dry-run
"""
import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (AskMaddi image cache; +https://askmaddi.com)"


def _is_ebay(identity):
    if (identity.get("image_source") or "") == "ebay_listing":
        return True
    return "ebayimg.com" in (identity.get("image_thumb") or "")


def _clean_hero_ids(hero_registry_path):
    """card_ids that already carry a clean Wikipedia/Commons hero — skip them."""
    try:
        reg = json.loads(Path(hero_registry_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {cid for cid, e in (reg.get("heroes", {}) or {}).items()
            if isinstance(e, dict) and e.get("url")}


def _load_registry(path):
    try:
        reg = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        reg = {}
    reg.setdefault("_description",
                   "card_id -> self-hosted copy of the eBay fallback image; "
                   "served at build by build_site.apply_selfhost_registry ONLY "
                   "when no clean hero exists")
    reg.setdefault("images", {})
    return reg


def _fetch(url, dest, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    if not data or len(data) < 1024:
        raise ValueError(f"suspiciously small ({len(data)} bytes)")
    dest.write_bytes(data)
    return len(data)


def run(cards_dir, images_dir, registry_path, hero_registry_path,
        force=False, dry_run=False, delay=0.5, only=None, now=None):
    cards_dir, images_dir = Path(cards_dir), Path(images_dir)
    clean = _clean_hero_ids(hero_registry_path)
    reg = _load_registry(registry_path)
    images = reg["images"]

    done, skipped_clean, skipped_have, failed, not_ebay = [], [], [], [], 0
    for fp in sorted(cards_dir.glob("*.json")):
        card = json.loads(fp.read_text(encoding="utf-8"))
        cid = card.get("card_id") or fp.stem
        if only and cid not in only:
            continue
        identity = card.get("identity", {}) or {}
        if not _is_ebay(identity):
            not_ebay += 1
            continue
        if cid in clean:
            skipped_clean.append(cid)
            continue
        if cid in images and not force:
            skipped_have.append(cid)
            continue
        src = identity.get("image_thumb") or ""
        rel = f"{images_dir.name}/{cid}.jpg"           # e.g. heroes/<id>.jpg
        # keep the registry file path relative to browser/ (served as /<rel>)
        served_rel = str(Path(*images_dir.parts[images_dir.parts.index('browser') + 1:]) / f"{cid}.jpg") \
            if 'browser' in images_dir.parts else rel
        if dry_run:
            done.append((cid, src, served_rel))
            continue
        try:
            images_dir.mkdir(parents=True, exist_ok=True)
            n = _fetch(src, images_dir / f"{cid}.jpg")
            images[cid] = {"file": served_rel, "source_url": src,
                           "bytes": n, "fetched_at": now or ""}
            done.append((cid, src, served_rel))
            time.sleep(delay)
        except Exception as e:                          # noqa: BLE001
            failed.append((cid, str(e)))

    if not dry_run and done:
        reg["updated_at"] = now or ""
        Path(registry_path).write_text(
            json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"considered eBay cards: {len(done) + len(skipped_clean) + len(skipped_have) + len(failed)}")
    print(f"self-hosted:      {len(done)}{' (dry-run, no write)' if dry_run else ''}")
    for cid, _src, rel in done[:60]:
        print(f"   + {cid} -> /{rel}")
    if skipped_clean:
        print(f"skipped (clean hero): {len(skipped_clean)}")
    if skipped_have:
        print(f"skipped (already self-hosted): {len(skipped_have)} (use --force to refetch)")
    if failed:
        print(f"FAILED: {len(failed)}")
        for cid, err in failed:
            print(f"   ! {cid}: {err}")
    return 0 if not failed else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--cards-dir", required=True)
    ap.add_argument("--images-dir", required=True,
                    help="where to write the .jpg files, e.g. browser/images/heroes")
    ap.add_argument("--registry", required=True,
                    help="self-host registry to write/merge, e.g. data/selfhost_images.json")
    ap.add_argument("--hero-registry", default="data/hero_images.json",
                    help="clean hero registry; its cards are skipped (they win)")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only", default=None, help="comma-separated card_ids")
    ap.add_argument("--delay", type=float, default=0.5)
    ap.add_argument("--now", default=None, help="ISO timestamp to stamp (tests pass fixed)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    only = {x.strip() for x in args.only.split(",")} if args.only else None
    return run(args.cards_dir, args.images_dir, args.registry, args.hero_registry,
               force=args.force, dry_run=args.dry_run, delay=args.delay,
               only=only, now=args.now)


if __name__ == "__main__":
    raise SystemExit(main())
