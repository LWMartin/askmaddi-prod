"""
skus.json registry writer — eBay-resolved canonical identity layer.
===================================================================
Demand-factory Stage 1 (maddi-skus-registry spec). Holds eBay-resolved
canonical identity ALONGSIDE the editorial contamination registry, bridged by
a `contamination_key` field — it does NOT subsume editorial judgment
(decision #1). One entry per card slug; lossless identity from resolve()
(decision #2).

The writer is the primitive both the back-fill tool and the future
/ebay/resolve route call. Per decision #3 it is:
  - idempotent      same tapped item twice -> no fork, no double-write
  - atomic-write    temp file + os.replace, never a half-written registry
  - concurrency-tolerant   last-write-wins; two near-simultaneous resolves of
                           one SKU converge to one entry
Internal join key is `epid or slug` (locked slug-rule decision): epid is the
preferred machine key but can be null, so slug is the universal fallback.

skus.json lives at askmaddi-prod data/skus.json, beside data/cards/.
"""
import json
import os
import tempfile
import time
from pathlib import Path

SKUS_PATH = Path(__file__).parent.parent / 'data' / 'skus.json'
SCHEMA_VERSION = '0.1.0'


def _empty_registry():
    return {
        '_description': (
            'eBay-resolved canonical identity layer. Bridges to the editorial '
            'contamination registry via contamination_key; does not subsume it.'
        ),
        'version': SCHEMA_VERSION,
        'as_of': time.strftime('%Y-%m-%d', time.gmtime()),
        'skus': {},
    }


def load_registry(path=SKUS_PATH):
    """Return the registry dict, or a fresh empty one if absent.

    Tolerant of a missing file (first run) but not of a corrupt one — a
    malformed skus.json is a real error the caller should see, not silently
    overwrite.
    """
    path = Path(path)
    if not path.exists():
        return _empty_registry()
    return json.loads(path.read_text(encoding='utf-8'))


def _atomic_write(registry, path=SKUS_PATH):
    """Write registry to `path` atomically (temp in same dir + os.replace).

    Same-directory temp guarantees os.replace is a same-filesystem rename
    (atomic), so a reader never observes a half-written file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.skus-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(registry, fh, indent=2, ensure_ascii=False)
            fh.write('\n')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _same_identity(a, b):
    """True if two entries carry the same canonical identity (idempotency check).

    Compares the stable identity keys — epid + legacy_item_id + mpn. price_seen
    and resolved_at are expected to drift between resolves of the same item and
    are NOT part of identity; comparing them would defeat idempotency.
    """
    ai, bi = a.get('identity', {}), b.get('identity', {})
    keys = ('epid', 'legacy_item_id', 'mpn')
    return all(ai.get(k, '') == bi.get(k, '') for k in keys)


def build_entry(slug, vendor, model, category, contamination_key,
                resolved, amazon_asin=None,
                source='resolved', minted_needs_review=False):
    """Assemble one skus.json entry from a resolve() result.

    `resolved` is the dict returned by ebay_api.resolve():
      {'identity': {...}, 'affiliate_url': ..., '_raw': ...}
    The entry persists the lossless identity block + the affiliate/editorial
    bridges. `_raw` is intentionally NOT persisted into skus.json (it's large
    and the schema fields already capture what downstream needs); callers that
    want it can archive it separately.

    PROVENANCE (minting wire, 2026-06-30):
      source : 'resolved' | 'generated'
        How this entry's slug came to be. 'resolved' (default) = a tapped/
        hand-curated slug that was already a registry fact when enriched — the
        historical path, and what the four frozen entries implicitly are.
        'generated' = the slug was MINTED by slug_normalizer.resolve_slug from a
        demand-discovered vendor/model that had no prior registry entry. The
        publish review surfaces this so Lee knows to look harder at a machine-
        minted card before it goes live.
      minted_needs_review : bool
        True only on the mint path (a generated slug, or a mint whose category
        came back unknown). Rides into the entry so the /admin publish view can
        badge it. The publish gate is the air-gap review; this flag tells the
        reviewer WHICH cards are machine-originated. The four frozen entries lack
        the field entirely -> read as the trusted default (False) via .get().
    """
    identity = dict(resolved.get('identity', {}))
    return {
        'contamination_key': contamination_key,
        'vendor': vendor,
        'model': model,
        'category': category,
        'identity': identity,
        'affiliate': {
            'ebay_epn_url': resolved.get('affiliate_url', ''),
            'amazon_asin': amazon_asin,
        },
        'source': source,
        'minted_needs_review': minted_needs_review,
        'resolved_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }


def upsert(slug, entry, path=SKUS_PATH):
    """Insert or update one SKU entry, idempotently and atomically.

    Returns one of: 'created', 'updated', 'unchanged'.
      - created    slug was absent
      - unchanged  slug present with the same canonical identity (idempotent
                   re-resolve) -> registry not rewritten, no churn
      - updated    slug present but identity differs (the item genuinely
                   changed) -> last-write-wins

    The 'unchanged' short-circuit is what makes a double-resolve a no-op: we
    skip the write entirely, so file mtime and content are stable.
    """
    registry = load_registry(path)
    skus = registry.setdefault('skus', {})
    existing = skus.get(slug)

    if existing is not None and _same_identity(existing, entry):
        return 'unchanged'

    status = 'updated' if existing is not None else 'created'
    skus[slug] = entry
    registry['as_of'] = time.strftime('%Y-%m-%d', time.gmtime())
    _atomic_write(registry, path)
    return status
