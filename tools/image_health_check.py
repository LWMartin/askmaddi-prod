"""
image_health_check.py — nightly image rot detection (images-on-spine D5).
=========================================================================
One script, two checks over the PUBLISHED cards, ~14 HEAD requests/night:

  1. MISMATCH  — the rendered image URL (browser/cards-manifest.json, the
     structured record build_site emits at publish) differs from the spine's
     CURRENT pick for that slug. Published HTML bakes the URL, so a spine
     image change (re-resolve on a new listing, a gate override, the
     image_catalog backfill arriving) needs a re-render to reach the site.
  2. DEAD URL  — the rendered URL itself HEAD-checks non-200 (eBay purges
     ended-listing images; rare same-listing death).

Either finding drops a signal file into the existing alert path
(~/.askmaddi-bot/signals — the weekly sweep's convention) and logs a line.
DELIBERATELY NOT auto-republish: publish stays behind the human gate, the
air gap is structural (spec D5). The flag is the whole job.

Spine pick mirrors spine_identity()'s image logic (phantom-ops
aggregator-build/assemble_card.py): override.image_thumb > identity.
image_catalog > identity.image. Kept self-contained here — a read-only
nightly checker should not import across the repo seam; if the pick logic
ever changes there, change it here (both cite this note).

No caching/mirroring of eBay images: hotlinking i.ebayimg.com in
EPN-affiliate cards is the program's intended use; self-hosted copies are
a ToS liability we don't need.

Usage:
    python3 tools/image_health_check.py \
        --skus data/skus.json \
        --manifest browser/cards-manifest.json \
        --signals /home/askmaddi/.askmaddi-bot/signals
Exit codes: 0 = all healthy (or nothing to check), 1 = findings flagged,
2 = cannot run (missing inputs).
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:          # pragma: no cover - requests is a repo dependency
    requests = None

HEAD_TIMEOUT = 10


def spine_pick(entry):
    """The spine's current image pick for a slug (mirror of spine_identity).

    override.image_thumb > identity.image_catalog > identity.image, '' when
    the spine holds no image at all.
    """
    overrides = entry.get('overrides') or {}
    ovr = (overrides.get('image_thumb') or '').strip()
    if ovr:
        return ovr
    ident = entry.get('identity') or {}
    return ((ident.get('image_catalog') or '').strip()
            or (ident.get('image') or '').strip())


def head_ok(url, head=None):
    """True iff a HEAD on `url` answers 200. `head` is injectable for tests."""
    if head is None:
        if requests is None:
            raise RuntimeError('requests unavailable and no head injected')

        def head(u):
            return requests.head(u, timeout=HEAD_TIMEOUT,
                                  allow_redirects=True).status_code
    try:
        return head(url) == 200
    except Exception:        # noqa: BLE001 — network failure IS the finding
        return False


def check(skus_path, manifest_path, head=None):
    """Run both checks. Returns a list of finding dicts (empty = healthy).

    Iterates the PUBLISHED record (manifest entries) — cards not yet
    published have no baked URL to rot. A published card whose slug has
    left the spine is itself a finding (integrity, not image, but the
    nightly is the eye that would notice).
    """
    skus = json.loads(Path(skus_path).read_text(encoding='utf-8'))
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    entries = skus.get('skus') or {}
    cards = manifest.get('cards') if isinstance(manifest, dict) else manifest
    findings = []

    for card in (cards or []):
        slug = card.get('card_id') or card.get('slug') or ''
        rendered = (card.get('image_thumb') or '').strip()
        entry = entries.get(slug)
        if entry is None:
            findings.append({'slug': slug, 'kind': 'spine-missing',
                             'detail': 'published card has no spine entry'})
            continue
        current = spine_pick(entry)
        if current != rendered:
            findings.append({
                'slug': slug, 'kind': 'mismatch',
                'detail': 'rendered image differs from spine pick — '
                          're-render through the gate to refresh',
                'rendered': rendered, 'spine': current})
        if rendered and not head_ok(rendered, head=head):
            findings.append({
                'slug': slug, 'kind': 'dead-url',
                'detail': 'rendered image URL no longer answers 200',
                'rendered': rendered})
    return findings


def write_signal(findings, signals_dir):
    """Drop ONE signal file carrying all of tonight's findings (the weekly
    sweep's convention: JSON, timestamped name, append-only directory)."""
    d = Path(signals_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f'image-health-{int(time.time())}.json'
    path.write_text(json.dumps({
        'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'tool': 'image_health_check',
        'findings': findings,
    }, indent=2), encoding='utf-8')
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument('--skus', required=True, help='Path to data/skus.json')
    ap.add_argument('--manifest', required=True,
                    help='Path to browser/cards-manifest.json')
    ap.add_argument('--signals', required=True,
                    help='Signal directory (the alert path)')
    args = ap.parse_args(argv)

    for p in (args.skus, args.manifest):
        if not Path(p).exists():
            print(f'image_health_check: missing input {p}', file=sys.stderr)
            return 2

    findings = check(args.skus, args.manifest)
    if not findings:
        print('image_health_check: all published images healthy')
        return 0
    path = write_signal(findings, args.signals)
    for f in findings:
        print(f"image_health_check: [{f['kind']}] {f['slug']} — {f['detail']}")
    print(f'image_health_check: {len(findings)} finding(s) -> {path}')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
