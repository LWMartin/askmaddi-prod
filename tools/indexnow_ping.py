#!/usr/bin/env python3
"""
indexnow_ping.py — submit the sitemap's URLs to IndexNow after a publish.
=========================================================================
Phase 0 of maddi-distribution v2.0 (2026-07-17). IndexNow feeds Bing's index,
and Bing feeds Copilot and ChatGPT Search retrieval — the cheap back door to
the ~87%-of-AI-referrals engine while the domain is too young for organic
authority. Google ignores IndexNow; that's fine, this was never for Google.

Design:
  - Reads URLs from browser/sitemap.xml (the derived artifact build_site
    already maintains) — no second URL inventory to drift.
  - Key: IndexNow requires a key file served at the site root. Ours is
    browser/<key>.txt (committed; the key is not a secret — its only function
    is proving root control of the host). The key is DISCOVERED from the
    file, so rotating it is: replace the file, done. Exactly one <32-hex>.txt
    file may exist in browser/; zero or multiple is a loud error.
  - One POST, all URLs (protocol allows up to 10,000 per submission).
  - Fail SOFT by default (exit 0 on network/HTTP failure, loud on stderr):
    this runs at the tail of publish flows, and indexing hints must never
    fail a publish. --strict flips that for hand runs.

Usage:
    python3 tools/indexnow_ping.py                 # sitemap → api.indexnow.org
    python3 tools/indexnow_ping.py --dry-run       # print payload, no network
    python3 tools/indexnow_ping.py --strict        # nonzero exit on failure
"""
import argparse
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HOST = "askmaddi.com"
ENDPOINT = "https://api.indexnow.org/indexnow"
_KEYFILE_RE = re.compile(r'^[0-9a-f]{32}\.txt$')
_SM_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def find_key(browser_dir):
    """The single <32-hex>.txt at site root IS the key. 0 or 2+ = loud error."""
    hits = [p for p in Path(browser_dir).glob("*.txt")
            if _KEYFILE_RE.match(p.name)]
    if len(hits) != 1:
        raise SystemExit(
            f"indexnow: expected exactly one 32-hex .txt key file in "
            f"{browser_dir}, found {len(hits)}: {[p.name for p in hits]}")
    return hits[0].stem


def sitemap_urls(sitemap_path):
    root = ET.parse(sitemap_path).getroot()
    urls = [el.text.strip() for el in root.findall(".//sm:loc", _SM_NS)
            if el.text and el.text.strip()]
    if not urls:
        raise SystemExit(f"indexnow: no <loc> entries in {sitemap_path}")
    return urls


def submit(urls, key, dry_run=False):
    payload = {
        "host": HOST,
        "key": key,
        "keyLocation": f"https://{HOST}/{key}.txt",
        "urlList": urls,
    }
    body = json.dumps(payload).encode("utf-8")
    if dry_run:
        print(json.dumps(payload, indent=2))
        return True
    req = urllib.request.Request(
        ENDPOINT, data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        # IndexNow success is 200 (processed) or 202 (accepted).
        print(f"indexnow: HTTP {resp.status} — {len(urls)} url(s) submitted")
        return resp.status in (200, 202)


def main():
    ap = argparse.ArgumentParser(description="Submit sitemap URLs to IndexNow.")
    ap.add_argument("--browser-dir", default="browser",
                    help="Site root containing sitemap.xml and the key file.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the payload; no network.")
    ap.add_argument("--strict", action="store_true",
                    help="Nonzero exit on failure (default: soft-fail).")
    args = ap.parse_args()

    browser = Path(args.browser_dir)
    try:
        key = find_key(browser)
        urls = sitemap_urls(browser / "sitemap.xml")
        ok = submit(urls, key, dry_run=args.dry_run)
        return 0 if ok else (1 if args.strict else 0)
    except SystemExit:
        raise
    except Exception as e:  # network, parse — never fail a publish by default
        print(f"indexnow: soft-fail — {e}", file=sys.stderr)
        return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
