"""
image_health_check.py — nightly image rot detection (images-on-spine D5).
=========================================================================
One script, three checks — two over the PUBLISHED cards (~14 HEAD requests)
and one over the SPINE (~14 gateway resolves):

  1. MISMATCH  — the rendered image URL (browser/cards-manifest.json, the
     structured record build_site emits at publish) differs from the spine's
     CURRENT pick for that slug. Published HTML bakes the URL, so a spine
     image change (re-resolve on a new listing, a gate override, the
     image_catalog backfill arriving) needs a re-render to reach the site.
  2. DEAD URL  — the rendered URL itself HEAD-checks non-200 (eBay purges
     ended-listing images; rare same-listing death).

  3. DEAD LISTING — the SKU's eBay listing no longer resolves (getItem 404).
     Walks the spine, not the manifest: an unpublished SKU with a dead
     listing still matters, because a card built on a dead anchor inherits
     the rot. THREE-STATE (alive/dead/unknown) — a throttle or an outage is
     `unknown`, recorded but never alerted, because check 2's "any failure
     is the finding" posture is right for an image and wrong for a listing.
     Requires --gateway; omitted, the check is skipped and said so.

Any finding drops a signal file into the existing alert path
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
RESOLVE_TIMEOUT = 20     # gateway -> eBay getItem; slower than a HEAD


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


def listing_state(item_id, resolve):
    """Return 'alive', 'dead', or 'unknown' for one eBay legacy item id.

    THREE states, deliberately. `head_ok` above collapses any failure into a
    finding because an image you cannot fetch is an image the reader cannot
    see — the failure IS the symptom. Liveness is the opposite: a throttled
    night or an expired token says nothing about whether the listing exists.

    Collapsing to two fails in whichever direction you pick. Treat transient
    as dead and one 429 flags all 14 SKUs, training the reader to ignore the
    signal path. Treat transient as alive and a genuinely dead listing rots
    silently, which is the manfrotto-befree case this check exists for.

    Only a 404 from eBay's getItem means the listing is gone. Everything else
    that is not a clean 200 is `unknown` and is recorded, never alerted.
    """
    if not item_id:
        return 'unknown'
    try:
        status, body = resolve(f'v1|{item_id}|0')
    except Exception:            # noqa: BLE001 — a failed call is not a death
        return 'unknown'
    if status == 200:
        return 'alive'
    # The route answers 502 for every upstream failure and carries the real
    # cause in `upstream_status` (added 2026-07-30 for exactly this caller).
    if (body or {}).get('upstream_status') == 404:
        return 'dead'
    return 'unknown'


def _gateway_resolver(gateway):
    """Default resolver: the local gateway's /ebay/resolve. Returns (status, body)."""
    if requests is None:
        raise RuntimeError('requests unavailable and no resolver injected')

    def resolve(item_id):
        r = requests.get(f'{gateway.rstrip("/")}/ebay/resolve',
                         params={'item_id': item_id}, timeout=RESOLVE_TIMEOUT)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}
    return resolve


def check_listings(skus_path, resolve):
    """Walk the SPINE checking each SKU's eBay listing still resolves.

    Iterates skus.json rather than the manifest the image checks use: an
    unpublished SKU with a dead listing still matters, because a card built on
    a dead anchor inherits the rot. Returns (findings, inconclusive) — the
    caller alerts on the first and records the second.
    """
    skus = json.loads(Path(skus_path).read_text(encoding='utf-8'))
    findings, inconclusive = [], []

    for slug, entry in (skus.get('skus') or {}).items():
        item_id = ((entry.get('marketplace_ids') or {})
                   .get('ebay_legacy_item_id') or '').strip()
        if not item_id:
            inconclusive.append({
                'slug': slug, 'kind': 'listing-unknown',
                'detail': 'no ebay_legacy_item_id on the spine entry'})
            continue
        state = listing_state(item_id, resolve)
        if state == 'dead':
            findings.append({
                'slug': slug, 'kind': 'dead-listing',
                'detail': 'eBay listing no longer resolves (getItem 404) — '
                          'the spine anchor is gone; re-resolve before this '
                          'card is rebuilt or republished',
                'item_id': item_id})
        elif state == 'unknown':
            inconclusive.append({
                'slug': slug, 'kind': 'listing-unknown',
                'detail': 'liveness could not be established this run '
                          '(throttle, outage, or credential problem) — '
                          'NOT evidence the listing is gone',
                'item_id': item_id})
    return findings, inconclusive


def write_signal(findings, signals_dir, inconclusive=None):
    """Drop ONE signal file carrying all of tonight's findings (the weekly
    sweep's convention: JSON, timestamped name, append-only directory).

    `inconclusive` is recorded alongside but is NOT a finding: it must not
    raise the exit code, or a throttled night pages the reader. It is written
    so a PERSISTENT unknown (creds broken for a week) is visible in the
    signal history rather than vanishing into a clean-looking run."""
    d = Path(signals_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f'image-health-{int(time.time())}.json'
    path.write_text(json.dumps({
        'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'tool': 'image_health_check',
        'findings': findings,
        'inconclusive': inconclusive or [],
    }, indent=2), encoding='utf-8')
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument('--skus', required=True, help='Path to data/skus.json')
    ap.add_argument('--manifest', required=True,
                    help='Path to browser/cards-manifest.json')
    ap.add_argument('--signals', required=True,
                    help='Signal directory (the alert path)')
    ap.add_argument('--gateway', default=None,
                    help='Local gateway base URL (e.g. http://127.0.0.1:5001). '
                         'When given, also walks the spine checking each SKU\'s '
                         'eBay listing still resolves. Omitted = image checks '
                         'only, so the tool stays usable offline.')
    args = ap.parse_args(argv)

    for p in (args.skus, args.manifest):
        if not Path(p).exists():
            print(f'image_health_check: missing input {p}', file=sys.stderr)
            return 2

    findings = check(args.skus, args.manifest)
    inconclusive = []
    if args.gateway:
        live_findings, inconclusive = check_listings(
            args.skus, _gateway_resolver(args.gateway))
        findings += live_findings
    else:
        print('image_health_check: no --gateway, skipping listing liveness')

    for i in inconclusive:
        print(f"image_health_check: [{i['kind']}] {i['slug']} — {i['detail']}")

    if not findings:
        # Inconclusive results still get written: a run where nothing could be
        # checked must not leave the same trace as a run where everything was
        # checked and was healthy.
        if inconclusive:
            path = write_signal([], args.signals, inconclusive)
            print(f'image_health_check: no findings, '
                  f'{len(inconclusive)} inconclusive -> {path}')
        else:
            print('image_health_check: all published images healthy')
        return 0
    path = write_signal(findings, args.signals, inconclusive)
    for f in findings:
        print(f"image_health_check: [{f['kind']}] {f['slug']} — {f['detail']}")
    print(f'image_health_check: {len(findings)} finding(s) -> {path}')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
