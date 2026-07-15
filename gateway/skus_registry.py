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
    """Write registry to `path` atomically (temp in same dir + os.replace),
    GROUP-READABLE.

    Same-directory temp guarantees os.replace is a same-filesystem rename
    (atomic), so a reader never observes a half-written file.

    CROSS-USER READ (found live 2026-07-15, canon-r6/r5 imageless cards):
    since images-on-spine step 4 (2026-07-09), factory builds running as
    phantomops READ this store via build_card --spine. mkstemp creates temps
    0600 and os.replace carries that mode onto the store, so any full rewrite
    by askmaddi (upsert, set_gtin, set_override, set_image_catalog) strips
    phantomops's read — and build_card's missing-spine path degrades
    SILENTLY to pre-spine cards (no image, no subcategory, no display_name).
    The 16:46Z image-sweep commit did exactly this. fchmod 0o640 keeps the
    store group-readable across every rewrite (the data/ dir's setgid bit
    keeps it in the pipeline group); group WRITE stays off — the spine is
    written by askmaddi alone, and that half of the old assumption still
    holds. Same class as work_queue's 0664 fix, one seam over.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.skus-', suffix='.tmp')
    try:
        os.fchmod(fd, 0o640)
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(registry, fh, indent=2, ensure_ascii=False)
            fh.write('\n')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ── Substrate shape accessors (Axis A/B, migration-tolerant) ───────────────
#
# The substrate migration hoists identity.gtin -> entry.gtin (Axis A anchor),
# identity.{epid,legacy_item_id} -> marketplace_ids.*, category -> facet, and
# identity.ebay_category_id -> marketplace_categories.* (spec:
# maddi-product-substrate, migration note). These accessors read the NEW shape
# first and fall back to the old one, so every reader stays correct across the
# pull-lag window and over a mixed registry (new-shape mints landing before
# migrate_substrate.py runs on the box). WRITERS are strictly new-shape; only
# reads tolerate both. Once the live spine is migrated and verified, the
# fallbacks are dead code that can be retired in a later cleanup.

def get_gtin(entry):
    """The Axis A identity anchor. New shape: entry.gtin; old: identity.gtin."""
    if 'gtin' in entry:
        return entry.get('gtin')
    return (entry.get('identity') or {}).get('gtin')


def get_facet(entry):
    """The authored role vocab (body/lens/support/...). New: facet; old: category."""
    if 'facet' in entry:
        return entry.get('facet')
    return entry.get('category')


def get_marketplace_id(entry, key):
    """A marketplace identity shadow. `key` in {'ebay_epid',
    'ebay_legacy_item_id', 'amazon_asin'}. Old-shape fallback maps to the
    identity-block names (epid / legacy_item_id) or affiliate.amazon_asin."""
    mids = entry.get('marketplace_ids')
    if isinstance(mids, dict):
        return mids.get(key)
    if key == 'amazon_asin':
        return (entry.get('affiliate') or {}).get('amazon_asin')
    old = {'ebay_epid': 'epid', 'ebay_legacy_item_id': 'legacy_item_id'}[key]
    return (entry.get('identity') or {}).get(old)


def get_marketplace_category(entry, key='ebay_category_id'):
    """A marketplace classification shadow (Axis B demoted evidence)."""
    mcats = entry.get('marketplace_categories')
    if isinstance(mcats, dict):
        return mcats.get(key)
    return (entry.get('identity') or {}).get(key)


def _same_identity(a, b):
    """True if two entries carry the same canonical identity (idempotency check).

    Compares the stable identity keys — epid + legacy_item_id + mpn. price_seen
    and resolved_at are expected to drift between resolves of the same item and
    are NOT part of identity; comparing them would defeat idempotency. Reads
    through the substrate accessors so old-shape and new-shape entries of the
    same product compare equal (upsert stays idempotent across the migration).
    """
    def _mpn(e):
        return (e.get('identity') or {}).get('mpn', '')
    return (get_marketplace_id(a, 'ebay_epid') == get_marketplace_id(b, 'ebay_epid')
            and get_marketplace_id(a, 'ebay_legacy_item_id')
                == get_marketplace_id(b, 'ebay_legacy_item_id')
            and _mpn(a) == _mpn(b))


def build_entry(slug, vendor, model, facet, contamination_key,
                resolved, amazon_asin=None,
                source='resolved', minted_needs_review=False):
    """Assemble one skus.json entry from a resolve() result — SUBSTRATE SHAPE.

    `resolved` is the dict returned by ebay_api.resolve():
      {'identity': {...}, 'affiliate_url': ..., '_raw': ...}
    The entry persists the identity block + the affiliate/editorial bridges.
    `_raw` is intentionally NOT persisted into skus.json (it's large and the
    schema fields already capture what downstream needs); callers that want it
    can archive it separately.

    SUBSTRATE (spec: maddi-product-substrate; formerly `category`, now `facet`
    — verbatim vocab, renamed param, three call sites updated):
      Axis A — `gtin` hoisted to the top-level anchor (from _extract_identity's
        write into resolved identity; null if unresolved). `gtin_provenance`
        STAYS in the identity block: the anchor is canonical, the receipt is
        evidence, and the architecture demotes evidence deliberately.
        identity.{epid, legacy_item_id} land in `marketplace_ids` as demoted
        per-marketplace shadows; affiliate.amazon_asin is mirrored there
        (affiliate block itself unchanged — it serves the affiliate flow).
      Axis B — `facet` is the authored role vocab; `unspsc` starts null until
        the Gemma mapper backfills; identity.ebay_category_id lands in
        `marketplace_categories`.
      `needs_review` — the substrate's per-entry contract: any unmapped anchor
        (gtin null | unspsc null) forces true. Distinct from the slug-level
        SlugResolution.needs_review and from minted_needs_review, which keep
        their existing meanings.

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
        True only on the mint path (a generated slug, or a mint whose facet
        came back unknown). Rides into the entry so the /admin publish view can
        badge it. The publish gate is the air-gap review; this flag tells the
        reviewer WHICH cards are machine-originated. The four frozen entries lack
        the field entirely -> read as the trusted default (False) via .get().
    """
    identity = dict(resolved.get('identity', {}))
    gtin = identity.pop('gtin', None)
    marketplace_ids = {
        'ebay_epid': identity.pop('epid', ''),
        'ebay_legacy_item_id': identity.pop('legacy_item_id', ''),
        'amazon_asin': amazon_asin,
    }
    marketplace_categories = {
        'ebay_category_id': identity.pop('ebay_category_id', ''),
    }
    unspsc = None
    return {
        'contamination_key': contamination_key,
        'vendor': vendor,
        'model': model,
        'gtin': gtin,
        'marketplace_ids': marketplace_ids,
        'unspsc': unspsc,
        'facet': facet,
        'marketplace_categories': marketplace_categories,
        'identity': identity,
        'affiliate': {
            'ebay_epn_url': resolved.get('affiliate_url', ''),
            'amazon_asin': amazon_asin,
        },
        'needs_review': gtin is None or unspsc is None,
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
    if existing is not None:
        # D3 (images-on-spine): overrides are the HUMAN layer — written only
        # by /admin set_override, never by resolve. A genuine identity change
        # replaces the entry wholesale, which without this carry-forward would
        # silently discard hand corrections. Refresh-proof by construction:
        # re-resolve rewrites identity.*, never overrides.*.
        prior_overrides = existing.get('overrides')
        if prior_overrides and not entry.get('overrides'):
            entry = dict(entry)
            entry['overrides'] = prior_overrides
    skus[slug] = entry
    registry['as_of'] = time.strftime('%Y-%m-%d', time.gmtime())
    _atomic_write(registry, path)
    return status


def set_gtin(slug, gtin, provenance, path=SKUS_PATH):
    """Surgically write the GTIN anchor + its receipt for one SKU. UPGRADE-ONLY.

    The GTIN second-pass sweep's writer (substrate Amendment A / step 5b).
    This exists as a SEPARATE function because upsert() cannot carry a
    gtin-only change: gtin is deliberately NOT one of the identity
    idempotency keys (epid, legacy_item_id, mpn), so upsert would return
    'unchanged' and silently skip the write. Widening upsert's keys would
    change idempotency semantics for all three mint paths — wrong fix.
    Spine writes stay in this module (resolve_sku doctrine), so the field
    writer lives here, not in the sweep tool.

    SUBSTRATE SHAPE: the anchor is written to the TOP-LEVEL `gtin` (Axis A);
    the receipt goes to identity.gtin_provenance (evidence layer). On a
    not-yet-migrated entry this yields a legal mixed shape the accessors read
    correctly, and migrate_substrate.py normalizes on its pass.

    UPGRADE-ONLY: refuses to touch an entry whose gtin anchor is already
    non-null (read through get_gtin, so an old-shape identity.gtin blocks the
    write exactly as a new-shape anchor does). A first-pass (L1) GTIN is never
    overwritten by a recovered one. `gtin` may be None with a provenance
    receipt — that persists a CONFLICT-DROP flag for /admin visibility while
    the anchor stays null (disagreement = CONFLICT, never silent-pick).

    ADJUDICATION-TERMINAL: also refuses an entry whose provenance carries an
    `adjudications` event (see adjudicate_gtin). A dismissed conflict has
    gtin=null but a human ruling on record — a later machine sweep must not
    clobber that ruling with a fresh wholesale provenance write. Human
    judgment is terminal against machine writes; append-only discipline.

    Returns: 'written' | 'skipped-has-gtin' | 'skipped-adjudicated' |
    'missing-slug'. Atomic via the same _atomic_write as upsert.
    """
    registry = load_registry(path)
    skus = registry.setdefault('skus', {})
    entry = skus.get(slug)
    if entry is None:
        return 'missing-slug'

    identity = entry.setdefault('identity', {})
    if get_gtin(entry):
        return 'skipped-has-gtin'
    existing_prov = identity.get('gtin_provenance')
    if isinstance(existing_prov, dict) and existing_prov.get('adjudications'):
        return 'skipped-adjudicated'

    entry['gtin'] = gtin
    identity.pop('gtin', None)      # single anchor location, no stale shadow
    identity['gtin_provenance'] = provenance
    registry['as_of'] = time.strftime('%Y-%m-%d', time.gmtime())
    _atomic_write(registry, path)
    return 'written'


def set_override(slug, field, value, path=SKUS_PATH):
    """Surgically write one card-identity override field for one SKU (D3).

    The gate's paste-to-replace writer (spec: maddi-images-on-spine). The
    writable hand-curation layer lives ON THE SPINE as a per-entry
    `overrides: {}` dict — written ONLY by /admin actions, read by
    spine_identity() as the top merge layer over the mapped identity. It is
    a SEPARATE writer for the same reason set_gtin is: upsert cannot carry
    an override-only change (overrides are deliberately not identity
    idempotency keys), and resolve must never author this layer.

    Field names are CARD-identity vocabulary (image_thumb, subcategory,
    year_introduced, ...) — the mapper merges them verbatim, so what the
    gate writes is exactly what the card build reads.

    `value=None` DELETES the override (the mapper treats None as absent
    anyway; deleting keeps the spine honest rather than accumulating null
    tombstones). Deleting the last override drops the empty dict.

    Returns: 'written' | 'cleared' | 'no-op' | 'missing-slug'.
    Atomic via the same _atomic_write as upsert.
    """
    if not field or not isinstance(field, str):
        raise ValueError('set_override requires a non-empty string field name')
    registry = load_registry(path)
    skus = registry.setdefault('skus', {})
    entry = skus.get(slug)
    if entry is None:
        return 'missing-slug'

    overrides = entry.setdefault('overrides', {})
    if value is None:
        if field not in overrides:
            if not overrides:
                entry.pop('overrides', None)
            return 'no-op'
        overrides.pop(field)
        if not overrides:
            entry.pop('overrides', None)
        status = 'cleared'
    else:
        if overrides.get(field) == value:
            return 'no-op'
        overrides[field] = value
        status = 'written'

    registry['as_of'] = time.strftime('%Y-%m-%d', time.gmtime())
    _atomic_write(registry, path)
    return status


def set_image_catalog(slug, url, provenance=None, path=SKUS_PATH):
    """Surgically write a rescued catalog image onto one SKU's identity block.

    The image-second-pass writer (sibling of set_gtin — spine writes stay in
    this module, evidence-gathering lives in image_secondpass.py). A targeted
    field write, NOT an upsert: image_catalog is deliberately not an identity
    idempotency key, so this never disturbs the 'unchanged' short-circuit and
    never forces a re-resolve — which is exactly why the F2 veteran backfill
    can land through here on entries whose bound listings are long dead.

    UPGRADE-ONLY: refuses to overwrite a non-empty identity.image_catalog —
    a resolve-time capture (or an earlier rescue) is never clobbered by a
    later sweep. HUMAN-TERMINAL: refuses an entry carrying an
    overrides.image_thumb hand-curation (the /admin paste-box outranks any
    machine write; same doctrine as GTIN adjudication terminality).

    `provenance` (the rescue receipt) lands additively at
    identity.image_provenance so a swept image is always distinguishable
    from a resolve-time one.

    Returns: 'written' | 'skipped-has-catalog' | 'skipped-override' |
    'missing-slug' | 'bad-url'. Atomic via the same _atomic_write as upsert.
    """
    if not url or not isinstance(url, str) or not url.strip():
        return 'bad-url'
    registry = load_registry(path)
    skus = registry.setdefault('skus', {})
    entry = skus.get(slug)
    if entry is None:
        return 'missing-slug'
    if ((entry.get('overrides', {}) or {}).get('image_thumb')):
        return 'skipped-override'
    identity = entry.setdefault('identity', {})
    if (identity.get('image_catalog') or '').strip():
        return 'skipped-has-catalog'

    identity['image_catalog'] = url.strip()
    if provenance is not None:
        identity['image_provenance'] = provenance
    registry['as_of'] = time.strftime('%Y-%m-%d', time.gmtime())
    _atomic_write(registry, path)
    return 'written'


ADJUDICATE_ACTIONS = ('assign', 'dismiss')

DISMISS_REASONS = ('variant_ambiguous', 'wrong_product', 'insufficient_evidence')


def adjudicate_gtin(slug, action, gtin=None, reason=None, actor='admin',
                    path=SKUS_PATH):
    """Resolve a GTIN conflict receipt by human judgment. APPEND-ONLY.

    The abstain->human contract's writing half. The original conflict receipt
    (chosen_source, conflict flag, observations, recovery block) is NEVER
    mutated — resolution is an event appended to `gtin_provenance.adjudications`
    (list, created on first use). Readers get the full history: what the
    machine saw, what it refused to decide, and what the human ruled.

    Actions:
      'assign'  — set identity.gtin to `gtin`. The value MUST be one of the
                  receipt's evidenced GTINs (the disagreeing set) — a hand
                  adjudication picks among presented evidence, it does not
                  introduce a new identity claim. Free-text GTINs are a
                  different, heavier operation this deliberately does not offer.
      'dismiss' — leave gtin null with a structured `reason`; the SKU falls to
                  the substrate's Gemma+title fallback identity (the designed
                  behavior for genuinely unresolvable cases, e.g. a slug that
                  is itself ambiguous between two real variants). The event is
                  the terminal marker: /admin stops rendering it and set_gtin
                  refuses machine re-writes over it.

    Preconditions (all fail closed with a status, never a partial write):
      entry exists; identity.gtin is null; provenance is a dict with
      conflict=True; no prior adjudication event (one ruling per receipt —
      re-adjudication is a registry-surgery operation, not an /admin one).

    Returns: 'assigned' | 'dismissed' | 'missing-slug' | 'not-in-conflict' |
    'already-adjudicated' | 'gtin-not-evidenced' | 'bad-action' | 'bad-reason'.
    """
    if action not in ADJUDICATE_ACTIONS:
        return 'bad-action'

    registry = load_registry(path)
    entry = registry.setdefault('skus', {}).get(slug)
    if entry is None:
        return 'missing-slug'

    identity = entry.setdefault('identity', {})
    prov = identity.get('gtin_provenance')
    if (get_gtin(entry) or not isinstance(prov, dict)
            or prov.get('conflict') is not True):
        return 'not-in-conflict'
    if prov.get('adjudications'):
        return 'already-adjudicated'

    event = {
        'action': action,
        'actor': actor,
        'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }

    if action == 'assign':
        evidenced = _evidenced_gtins(prov)
        if not gtin or gtin not in evidenced:
            return 'gtin-not-evidenced'
        event['gtin'] = gtin
        entry['gtin'] = gtin            # the null->value upgrade, human path
        identity.pop('gtin', None)      # single anchor location (substrate)
    else:  # dismiss
        if reason not in DISMISS_REASONS:
            return 'bad-reason'
        event['reason'] = reason

    prov.setdefault('adjudications', []).append(event)
    registry['as_of'] = time.strftime('%Y-%m-%d', time.gmtime())
    _atomic_write(registry, path)
    return 'assigned' if action == 'assign' else 'dismissed'


def _evidenced_gtins(prov):
    """The GTIN set a receipt actually evidences — the only legal assign
    targets. L2 receipts carry the gate's distinct_gtins; L1 receipts derive
    from valid observations (same derivation the conflict flag used)."""
    recovery = prov.get('recovery')
    if isinstance(recovery, dict) and recovery.get('distinct_gtins'):
        return {g for g in recovery['distinct_gtins'] if g}
    return {o.get('gtin14') for o in prov.get('observations', ())
            if isinstance(o, dict) and o.get('valid') and o.get('gtin14')}
