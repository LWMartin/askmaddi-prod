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


# ── Contamination-join resolution (structural guard, 2026-08-27) ──────────────
#
# contamination.json lives in the phantom-ops workspace (the gateway box checks
# out both repos; the factory reaches build_card.py by the same route). Overridable
# via env so tests/sandbox never depend on the box layout — mirrors card_factory's
# DEFAULT_BUILD_CARD.
DEFAULT_CONTAMINATION_JSON = (
    Path.home() / 'phantom-ops' / 'claude' / 'workspace'
    / 'aggregator-build' / 'fixtures' / 'manifests' / 'contamination.json'
)


def _generic_key(name):
    """'<name>-generic' in the registry's slug form, or None for empty input.
    Mirrors build_card._generic_key so registration and build agree by construction."""
    if not name or not name.strip():
        return None
    slug = name.strip().lower().replace(' ', '-').replace('_', '-')
    return f'{slug}-generic'


def _load_contamination_products(contam_path=None):
    """The contamination.json `products` map, or None if unreadable — the resolver
    then FAILS OPEN (an infra hiccup must never block a live mint; the nightly
    registry_join_check bridge stays the backstop)."""
    p = Path(contam_path or os.environ.get('ASKMADDI_CONTAMINATION_JSON')
             or DEFAULT_CONTAMINATION_JSON)
    try:
        return json.loads(p.read_text(encoding='utf-8')).get('products', {})
    except (OSError, ValueError):
        return None


def resolve_contamination_key(slug, vendor, facet, *, contam_path=None):
    """Resolve the contamination_key a NEW spine entry should STORE, via the same
    ladder build_card._require_contamination_join uses at build time:

        specific(slug)  ->  {vendor}-generic  ->  {facet}-generic

    so the stored key resolves BY CONSTRUCTION — the dangling self-name class (the
    15-key bridge break of 2026-08-27, `assert_joinable` built but never mounted)
    cannot recur. Returns (key, tier):

      'specific' | 'brand_generic' | 'facet_generic'
          `key` is a resolvable key to store. brand/facet tiers are coverage-debt
          (coarser relevance; a specific entry is worth authoring later).
      'unresolved'
          nothing in the ladder resolved; `key` echoes the slug. The caller must
          NOT store it silently — route to review so a human authors an entry.
      'unknown'
          contamination.json unreadable (infra); FAIL OPEN — `key` is the slug and
          the caller proceeds exactly as before (the nightly bridge still guards).
    """
    products = _load_contamination_products(contam_path)
    if products is None:
        return slug, 'unknown'
    if slug in products:
        return slug, 'specific'
    bg = _generic_key(vendor)
    if bg and bg in products:
        return bg, 'brand_generic'
    fg = _generic_key((facet or '').split('/')[0])
    if fg and fg in products:
        return fg, 'facet_generic'
    return slug, 'unresolved'


def build_entry(slug, vendor, model, facet, contamination_key,
                resolved, amazon_asin=None,
                source='resolved', minted_needs_review=False,
                contamination_tier=None):
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
    entry = {
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
    # Coverage-debt breadcrumb: a brand/facet-generic contamination_key means the
    # relevance gate runs at coarse (brand/category) precision — worth a specific
    # entry later. Recorded only when it's debt (a generic tier), so /admin can
    # surface the queue; a 'specific' resolve (the good case) adds no field.
    if contamination_tier in ('brand_generic', 'facet_generic'):
        entry['contamination_tier'] = contamination_tier
    return entry


def _merge_enrichment(existing, entry):
    """Carry enrichment layers across an identity-change replace. Returns a copy.

    upsert()'s 'updated' path replaces the entry wholesale because identity
    genuinely changed (typically an eBay listing rotation flipping
    legacy_item_id). But a fresh build_entry() carries ONLY what THIS
    morning's resolve fetched — every layer written by other authors would
    silently die in the replace. Found live 2026-07-16: one listing rotation
    erased the 7/15 image-sweep rescues (identity.image_provenance), collapsed
    gtin_provenance.observations to [], and would have erased adjudications
    and Gemma-backfilled unspsc had any existed on the rotated entries.

    Each rule below mirrors the doctrine its surgical writer already enforces
    — this function makes the bulldozer obey the same law as the scalpels:

    - overrides             HUMAN layer (D3, images-on-spine): /admin-only
                            author; resolve never writes it. Carried whenever
                            the incoming entry lacks one.
    - gtin + gtin_provenance  set_gtin doctrine: an existing non-null anchor
                            is NEVER overwritten by a machine pass, and an
                            adjudicated provenance (human ruling) is terminal.
                            A null incoming anchor never clobbers a non-null
                            existing one; an adjudicated existing provenance
                            survives regardless of what resolve found.
                            An incoming anchor that CONFLICTS with the
                            existing one does not silently win — the existing
                            anchor stays, and the incoming receipt is
                            preserved under gtin_provenance.superseded for
                            /admin visibility (disagreement = CONFLICT,
                            never silent-pick).
    - image_catalog + image_provenance  set_image_catalog doctrine: a capture
                            on the spine is never clobbered by a pass that
                            arrives empty-handed. An incoming NON-EMPTY
                            capture wins (fresh resolve-time evidence for the
                            new listing); an incoming empty one carries the
                            existing capture + its provenance forward.
    - unspsc                Gemma-mapper backfill (Axis B): build_entry always
                            emits null; a non-null existing classification
                            carries forward.
    - spec_surface          AUTHORED curation (R10, maddi-naming-register):
                            the per-SKU URL fragments that fill the brand
                            table's template. resolve never writes it, so an
                            incoming entry never carries one. Held as its OWN
                            key rather than inside `overrides` because that
                            dict is the top layer of the card-identity merge
                            (maddi-images-on-spine D1/D3) — a fragment placed
                            there reaches the reader-facing identity block.
                            One key per concern, as the four rules above.

    needs_review is recomputed after the merge so a carried gtin/unspsc
    clears the flag exactly as build_entry would have set it.
    """
    entry = dict(entry)
    identity = dict(entry.get('identity') or {})
    entry['identity'] = identity
    old_identity = existing.get('identity') or {}

    # HUMAN layer (existing behavior, D3)
    prior_overrides = existing.get('overrides')
    if prior_overrides and not entry.get('overrides'):
        entry['overrides'] = prior_overrides

    # AUTHORED curation layer (R10) — same doctrine as overrides: resolve is
    # not its author, so a fresh build_entry never carries one and the
    # existing layer survives the replace.
    prior_spec_surface = existing.get('spec_surface')
    if prior_spec_surface and not entry.get('spec_surface'):
        entry['spec_surface'] = prior_spec_surface

    # GTIN anchor + receipt
    old_gtin = get_gtin(existing)
    old_prov = old_identity.get('gtin_provenance')
    old_adjudicated = isinstance(old_prov, dict) and old_prov.get('adjudications')
    new_gtin = entry.get('gtin')
    if old_adjudicated or (old_gtin and not new_gtin):
        # Human ruling terminal, or machine arrived empty-handed against a
        # standing anchor: existing anchor + receipt survive intact.
        entry['gtin'] = old_gtin
        if old_prov is not None:
            identity['gtin_provenance'] = old_prov
    elif old_gtin and new_gtin and new_gtin != old_gtin:
        # Conflict: never silent-pick. Existing anchor stands; the fresh
        # receipt is preserved for /admin adjudication.
        entry['gtin'] = old_gtin
        merged = dict(old_prov) if isinstance(old_prov, dict) else {}
        merged['superseded'] = identity.get('gtin_provenance')
        merged['conflict'] = True
        identity['gtin_provenance'] = merged
    elif not new_gtin and isinstance(old_prov, dict) and (
            old_prov.get('observations') or old_prov.get('conflict')):
        # Null-anchor receipt (set_gtin doctrine: gtin may be None WITH a
        # provenance receipt — a persisted CONFLICT-DROP flag or gathered
        # evidence awaiting adjudication). An incoming empty-handed resolve
        # must not erase the /admin-visible receipt.
        new_prov = identity.get('gtin_provenance')
        incoming_empty = not (isinstance(new_prov, dict) and (
            new_prov.get('observations') or new_prov.get('conflict')))
        if incoming_empty:
            identity['gtin_provenance'] = old_prov

    # Catalog image + rescue receipt
    if not (identity.get('image_catalog') or '').strip():
        old_img = (old_identity.get('image_catalog') or '').strip()
        if old_img:
            identity['image_catalog'] = old_img
            if 'image_provenance' in old_identity:
                identity['image_provenance'] = old_identity['image_provenance']

    # Axis B classification
    if entry.get('unspsc') is None and existing.get('unspsc') is not None:
        entry['unspsc'] = existing['unspsc']

    entry['needs_review'] = entry.get('gtin') is None or entry.get('unspsc') is None
    return entry


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
        entry = _merge_enrichment(existing, entry)
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


# The vocabulary `overrides` may carry. CO-DECLARED with
# aggregator-build/assemble_card.py's CARD_IDENTITY_FIELDS, which is the read
# side; tools/backfill_spec_fragments.py refuses to run if the two disagree.
#
# Duplicated deliberately rather than imported: phantom-ops is not a dependency
# of this repo, and the set is safe to duplicate precisely BECAUSE it does not
# grow with the catalogue. Axes, facts, labels and aliases vary by product type
# and must never be copied; identity does not — a drone and a tripod carry
# these same nine fields. Adding a vertical does not touch this.
#
# WHY A GATE EXISTS AT ALL (2026-07-29): `overrides` is the top layer of the
# card-identity merge (spec maddi-images-on-spine, D1/D3), and that merge is
# field-generic by design. So the layer's contract — "these keys are card
# identity" — was held only by the /admin route being its sole writer. An
# operator tool writing through this generic API put a spec-surface fragment
# in, and it would have surfaced in the reader-facing identity block. The
# route restricted; nothing structural did. This is the structural half.
CARD_IDENTITY_FIELDS = frozenset({
    'display_name', 'brand', 'model', 'sku_alt_names',
    'category', 'subcategory', 'year_introduced',
    'image_thumb', 'image_source',
})


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
    if value is not None and field not in CARD_IDENTITY_FIELDS:
        # Clearing (value=None) stays open for ANY field, so a foreign key
        # written before this gate existed can still be removed. Only new
        # writes are constrained — a gate that also blocked cleanup would
        # strand exactly the entries it was added because of.
        raise ValueError(
            f'{field!r} is not a card-identity field, and `overrides` is the '
            f'top layer of the card-identity merge — writing it here would '
            f'put it in the rendered card. Authored non-identity curation '
            f'gets its own carried key (see _merge_enrichment). Known fields: '
            f'{sorted(CARD_IDENTITY_FIELDS)}')
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


def set_spec_surface(slug, fragments, path=SKUS_PATH):
    """Write the authored spec-surface fragments for one SKU (R10).

    A SEPARATE writer from set_override for the same reason set_override is
    separate from upsert: this layer has a different author and a different
    contract. The brand table (phantom-ops `spec_surfaces.py`) owns the host
    and the URL template; a SKU owes only the fragments that fill it.

    Its own top-level key, NOT a field inside `overrides`. That dict is the
    top layer of the card-identity merge (maddi-images-on-spine D1/D3), which
    merges field-generically, so a fragment placed there reaches the rendered
    identity block. The carry list already keeps four concerns in four keys;
    this is the fifth.

    `fragments=None` DELETES the layer. Returns:
    'written' | 'cleared' | 'no-op' | 'missing-slug'.
    """
    if fragments is not None and not isinstance(fragments, dict):
        raise ValueError('spec_surface fragments must be a dict or None')
    registry = load_registry(path)
    entry = (registry.setdefault('skus', {})).get(slug)
    if entry is None:
        return 'missing-slug'

    if fragments is None:
        if 'spec_surface' not in entry:
            return 'no-op'
        entry.pop('spec_surface')
        status = 'cleared'
    else:
        if entry.get('spec_surface') == fragments:
            return 'no-op'
        entry['spec_surface'] = fragments
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


def set_rebind_rejection(slug, receipt, path=SKUS_PATH):
    """Park a firewall-rejected rebind as a receipt on the standing entry.

    The rebind firewall's writer (sibling of set_gtin / set_image_catalog —
    spine writes stay in this module, judgment lives in rebind_firewall.py).
    The standing identity is NOT touched; the fresh-but-suspect identity is
    preserved at the top-level `rebind_rejected` key for /admin visibility,
    mirroring how GTIN conflicts persist a receipt rather than silent-pick.

    IDEMPOTENT: the resolve pass runs daily and will re-offer the same junk
    listing every morning — re-parking an identical rejection (same item_id,
    same signals) skips the write entirely so the daily pass never churns
    the file (same discipline as upsert's 'unchanged' short-circuit).

    Lifecycle: a later LAWFUL rebind replaces the entry wholesale via
    upsert, and `rebind_rejected` is neither in build_entry's shape nor in
    _merge_enrichment's carry list — the receipt dies with the identity it
    warned about, by construction.

    Returns: 'written' | 'unchanged' | 'missing-slug'.
    """
    registry = load_registry(path)
    skus = registry.setdefault('skus', {})
    entry = skus.get(slug)
    if entry is None:
        return 'missing-slug'

    standing = entry.get('rebind_rejected')
    if (isinstance(standing, dict)
            and standing.get('item_id') == receipt.get('item_id')
            and standing.get('signals') == receipt.get('signals')):
        return 'unchanged'

    entry['rebind_rejected'] = receipt
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
