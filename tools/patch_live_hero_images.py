#!/usr/bin/env python3
"""Surgically swap the eBay hero URL -> the self-hosted copy in the LIVE card
HTML, in place, touching nothing else.

WHY not just rebuild: the box's price crons patch live prices/dates straight into
browser/cards/**/index.html (never back into data/cards/*.json), so a rebuild
from JSON would REGRESS those prices. This tool changes ONLY the product image
URL — the exact i.ebayimg.com string recorded in data/selfhost_images.json — so
prices, dates and everything else the crons wrote stay intact. Mirrors how the
crons themselves operate (patch HTML, not JSON). See phantom-ops memory
askmaddi-prod-stale-cards-vs-browser and askmaddi-image-de-ebay-and-seo.

Ownership-safe: rewrites each file in place via open(..., 'w') (same inode), so
the file keeps its askmaddi owner even when run as root. Idempotent: a card
already pointing at the local image is skipped. Preview with --dry-run.

    python tools/patch_live_hero_images.py \
        --registry /opt/askmaddi-prod/data/selfhost_images.json \
        --site-root /opt/askmaddi-prod/browser [--dry-run]
"""
import argparse
import json
import re
import sys
from pathlib import Path

BASE_URL = "https://askmaddi.com"
# Any eBay-hosted image URL (the product photo). The "buy used" CTAs point at
# ebay.com/sch — a different host — so this never touches affiliate links.
EBAY_IMG = re.compile(r'https://i\.ebayimg\.com/[^\s"\'<>]+')


def run(registry_path, site_root, base_url=BASE_URL, dry_run=False):
    reg = json.loads(Path(registry_path).read_text(encoding="utf-8"))
    images = reg.get("images", {})
    site_root = Path(site_root)

    patched, already, missing_file, no_match = [], [], [], []
    for cid, entry in sorted(images.items()):
        rel = (entry or {}).get("file") or f"images/heroes/{cid}.jpg"
        new = f"{base_url}/{rel.lstrip('/')}"
        page = site_root / "cards" / cid / "index.html"
        if not page.exists():
            missing_file.append(cid)
            continue
        html = page.read_text(encoding="utf-8")
        # Replace whatever eBay image URL is CURRENTLY live (crons churn these when
        # a listing ends), not a stale recorded one — so drift can't leave a card
        # on eBay. All product-image slots share one URL, so this hits og:image,
        # twitter:image, JSON-LD image and the <img src> together.
        live_urls = set(EBAY_IMG.findall(html))
        if not live_urls:
            (already if new in html else no_match).append(cid)
            continue
        n = sum(html.count(u) for u in live_urls)
        if not dry_run:
            for u in live_urls:
                html = html.replace(u, new)
            page.write_text(html, encoding="utf-8")   # in place: owner preserved
        patched.append((cid, n))

    print(f"patched: {len(patched)}{' (dry-run, no write)' if dry_run else ''}")
    for cid, n in patched:
        print(f"   ~ {cid} ({n} occurrence{'s' if n != 1 else ''} -> local)")
    if already:
        print(f"already local: {len(already)}")
    if no_match:
        print(f"no eBay URL in live HTML (skipped): {len(no_match)}")
        print("   " + ", ".join(no_match))
    if missing_file:
        print(f"card HTML missing: {len(missing_file)}")
        print("   " + ", ".join(missing_file))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--registry", default="/opt/askmaddi-prod/data/selfhost_images.json")
    ap.add_argument("--site-root", default="/opt/askmaddi-prod/browser")
    ap.add_argument("--base-url", default=BASE_URL)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    return run(args.registry, args.site_root, base_url=args.base_url, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
