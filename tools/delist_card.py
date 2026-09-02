#!/usr/bin/env python3
"""Retire a card/SKU across every surface it touches. THE REMOVAL DOOR.

WHY THIS EXISTS
The card system only grew: mint a SKU, build a card, publish it — nothing ever
left. That held until GTIN collisions appeared: two SKUs sharing one barcode
because a variant scraped its sibling's code off a noisy eBay listing, or the
same product minted twice under two slugs. The spine's in-place writers cannot
fix a collision — the GTIN anchor is immutable-once-assigned, by design — so the
resolution is to drop the redundant twin. Dropping it cleanly means removing it
from every surface at once, and those surfaces have DIFFERENT owners and
DIFFERENT reconcile paths, which is exactly why an ad-hoc `rm` across seven
places is error-prone and this orchestrator exists.

THE SURFACES (and why they split into two modes)
  git-tracked, clean, deploy via `git pull`        --files-only
    - data/cards/<slug>.json            the card source
    - browser/cards/<slug>/             the built page (directory)
    - browser/images/heroes/<slug>.jpg  the self-hosted hero, if any
    - data/selfhost_images.json         the hero-overlay entry, if any
    - data/hero_images.json             the clean-hero entry, if any
  live-authoritative on the box, reconciled on its own path   --spine-only
    - data/skus.json                    the identity spine (skus_registry.delist)

skus.json is live-written by the gateway and drifts from git HEAD, so a commit
that also touched it would collide with the box's working tree on `git pull`.
Removing the tracked files (which do NOT drift) in one commit, and the spine
entry in a separate live mutation, keeps each surface on its own honest reconcile
path. Run --files-only in the work clone (commit + land); run --spine-only on the
box as the askmaddi user (the store's owner). Default does both, for a fully
local repo.

THE WORK QUEUE IS DELIBERATELY NOT MUTATED
A stale queue record cannot resurrect a delisted SKU: claim_next only claims
state=='resolved', and a retired twin is never in that state. The queue file is
also phantomops-owned — an askmaddi rewrite would strip the factory cron's write
bit (work_queue's 0664 seam). So this REPORTS the queue record's state as a
courtesy and leaves it. If a record is somehow 'resolved' it says so loudly;
clearing that is a phantomops-side op, not this tool's.

Idempotent: an absent surface is reported and skipped. Preview with --dry-run
(the default); --apply writes.

    python3 tools/delist_card.py --slug panasonic-s5-mirrorless --files-only
    python3 tools/delist_card.py --slug panasonic-s5-mirrorless --files-only --apply
    sudo -u askmaddi python3 tools/delist_card.py \
        --slug panasonic-s5-mirrorless --slug 'sony-a7r v' --spine-only --apply
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'gateway'))
import skus_registry  # noqa: E402
import work_queue  # noqa: E402


def _remove_image_registry_entry(reg_path, slug, apply):
    """Remove `slug` from an image registry. Handles both the wrapped shape
    ({'images': {slug: ...}, ...}, selfhost_images.json) and a flat {slug: ...}
    map (hero_images.json). Returns 'removed' | 'absent' | 'no-registry'.
    Preserves the file's wrapper keys and a trailing newline on write."""
    p = Path(reg_path)
    if not p.exists():
        return 'no-registry'
    reg = json.loads(p.read_text(encoding='utf-8'))
    container = reg['images'] if isinstance(reg, dict) and isinstance(reg.get('images'), dict) else reg
    if not isinstance(container, dict) or slug not in container:
        return 'absent'
    if apply:
        container.pop(slug)
        tmp = p.with_name(p.name + '.tmp')
        tmp.write_text(json.dumps(reg, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        tmp.replace(p)
    return 'removed'


def delist(slug, *, repo, skus_path, do_files, do_spine, apply):
    """Retire one slug across the selected surfaces. Returns a list of
    (surface, action, detail) tuples — the audit line for each surface."""
    repo = Path(repo)
    report = []

    if do_files:
        card_json = repo / 'data' / 'cards' / f'{slug}.json'
        if card_json.exists():
            if apply:
                card_json.unlink()
            report.append(('card-json', 'removed', str(card_json)))
        else:
            report.append(('card-json', 'absent', str(card_json)))

        html_dir = repo / 'browser' / 'cards' / slug
        if html_dir.exists():
            if apply:
                shutil.rmtree(html_dir)
            report.append(('card-html', 'removed', str(html_dir)))
        else:
            report.append(('card-html', 'absent', str(html_dir)))

        hero = repo / 'browser' / 'images' / 'heroes' / f'{slug}.jpg'
        if hero.exists():
            if apply:
                hero.unlink()
            report.append(('hero-jpg', 'removed', str(hero)))
        else:
            report.append(('hero-jpg', 'absent', str(hero)))

        report.append(('selfhost-reg',
                       _remove_image_registry_entry(repo / 'data' / 'selfhost_images.json', slug, apply),
                       'data/selfhost_images.json'))
        report.append(('hero-reg',
                       _remove_image_registry_entry(repo / 'data' / 'hero_images.json', slug, apply),
                       'data/hero_images.json'))

    if do_spine:
        if apply:
            status = skus_registry.delist(slug, str(skus_path))
        else:
            reg = json.loads(Path(skus_path).read_text(encoding='utf-8')) if Path(skus_path).exists() else {}
            status = 'delisted' if slug in (reg.get('skus') or {}) else 'missing-slug'
        report.append(('spine', status, str(skus_path)))

    # Work queue: report only, never mutate (see module docstring).
    queue_path = repo / 'data' / 'work_queue.json'
    rec = work_queue.get(slug, path=str(queue_path)) if queue_path.exists() else None
    if rec:
        state = rec.get('state')
        note = (' !! RESOLVED and claimable — clear on the phantomops side'
                if state == 'resolved' else ' (inert: claim_next only takes resolved)')
        report.append(('work-queue', f'state={state}{note}', 'left in place'))
    else:
        report.append(('work-queue', 'no record', ''))

    return report


def main(argv=None):
    repo_default = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument('--slug', action='append', default=[], required=True,
                    help='Card/SKU slug to retire. Repeatable.')
    ap.add_argument('--apply', action='store_true',
                    help='Actually remove. Without it this only reports (dry-run).')
    ap.add_argument('--files-only', action='store_true',
                    help='Only the git-tracked file surfaces (commit + land these).')
    ap.add_argument('--spine-only', action='store_true',
                    help='Only the live spine entry (run as askmaddi on the box).')
    ap.add_argument('--repo', default=str(repo_default),
                    help=f'Repo root for the file surfaces (default: {repo_default}).')
    ap.add_argument('--skus-path', default=None,
                    help='Spine path to mutate (default: <repo>/data/skus.json).')
    args = ap.parse_args(argv)

    if args.files_only and args.spine_only:
        ap.error('--files-only and --spine-only are mutually exclusive (omit both for the full retirement).')
    do_files = not args.spine_only
    do_spine = not args.files_only
    skus_path = args.skus_path or str(Path(args.repo) / 'data' / 'skus.json')

    mode = 'APPLY' if args.apply else 'dry-run (no writes)'
    surfaces = ('files+spine' if do_files and do_spine else 'files' if do_files else 'spine')
    print(f'repo    : {args.repo}')
    print(f'spine   : {skus_path}')
    print(f'surfaces: {surfaces}')
    print(f'mode    : {mode}\n')

    for slug in args.slug:
        print(f'== {slug} ==')
        for surface, action, detail in delist(
                slug, repo=args.repo, skus_path=skus_path,
                do_files=do_files, do_spine=do_spine, apply=args.apply):
            print(f'  {surface:14} {action:12} {detail}')
        print()

    if not args.apply:
        print('Dry run — nothing written. Re-run with --apply.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
