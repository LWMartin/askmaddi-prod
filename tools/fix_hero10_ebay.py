#!/usr/bin/env python3
"""One-shot: correct gopro-hero10's eBay category id 31388 -> 11724.

gopro-hero10 was minted under eBay 31388 (Digital Cameras -> 'body'), but it is
an action cam (facet 'action_cam'). eBay 11724 (Camcorders) is where action cams
live and is what the sibling gopro-hero6 already carries; ebay_category_map now
maps 11724 -> action_cam, so this makes the (id, facet) pair agree. Written
through skus_registry's atomic writer (never hand-rolled). Idempotent: a re-run
after the fix is a no-op. Run as the askmaddi user on the /opt spine.
"""
import sys
sys.path.insert(0, '/opt/askmaddi-prod/gateway')
import skus_registry as R

SLUG = 'gopro-hero10'
NEW_ID = '11724'

reg = R.load_registry(R.SKUS_PATH)
e = (reg.get('skus') or {}).get(SLUG)
if e is None:
    print(f'{SLUG}: missing-slug — nothing to do')
    raise SystemExit(0)

before = R.get_marketplace_category(e, 'ebay_category_id')
if before == NEW_ID:
    print(f'{SLUG}: already {NEW_ID} — no-op')
    raise SystemExit(0)

mc = e.setdefault('marketplace_categories', {})
mc['ebay_category_id'] = NEW_ID
# If an old-shape leaf lingers under identity, align it so the accessor can't
# read a stale value on any reader that still prefers the old shape.
ident = e.get('identity')
if isinstance(ident, dict) and 'ebay_category_id' in ident:
    ident['ebay_category_id'] = NEW_ID

R._atomic_write(reg, R.SKUS_PATH)

after = R.get_marketplace_category(R.load_registry(R.SKUS_PATH)['skus'][SLUG],
                                   'ebay_category_id')
print(f'{SLUG}: ebay_category_id {before!r} -> {after!r}')
assert after == NEW_ID, 'write did not land'
