"""
image_secondpass.py — catalog-image rescue for spine entries stuck on seller photos.
================================================================================
Option 2 of the image-sourcing discussion (2026-07-15), architectural sibling of
gtin_secondpass: a bounded, precision-biased second look at the marketplace when
the FIRST resolve didn't yield what the spine wants.

THE GAP THIS CLOSES:
  The resolve path captures identity.image_catalog only when the BOUND listing's
  getItem payload carries product.imageUrls (the eBay CATALOG stock shot). When
  the bound listing lacks catalog association — common for accessories, or any
  seller-photo-only listing — the card falls back to the seller's open-box
  gallery photo (image_source 'ebay_listing'), and NOTHING ever goes looking
  for the stock shot. This module goes looking: one candidate search, up to
  `max_resolves` getItem calls, first catalog image that passes the
  wrong-product firewall wins.

WHY THIS ALSO RETIRES THE F2 FORCED-RE-RESOLVE PLAN:
  F2's motivation was that upsert's 'unchanged' short-circuit means a stable
  veteran entry never receives image_catalog. Forcing re-resolve fights the
  idempotency design and dies on dead listings (used goods sell). This module
  sidesteps both: it writes through a TARGETED field writer
  (skus_registry.set_image_catalog, the set_gtin pattern), so no upsert churn,
  and it searches the live market rather than re-touching the possibly-dead
  bound listing. Veterans and fresh mints take the identical path.

PRECISION BIAS (same doctrine as the GTIN sweep):
  - Candidates without an epid are skipped — catalog images live on
    catalog-associated listings; an unassociated listing can only offer
    another seller photo, which we already have.
  - If the entry has its own epid, an epid-equal candidate outranks all others
    (strongest identity match available).
  - Every accepted candidate must pass the clause-4 token firewall
    (gtin_secondpass._token_matches) against the entry's mpn/model — a clean
    stock image of the WRONG product is worse than an honest photo of the
    right one.
  - Human curation is terminal: an entry with overrides.image_thumb is never
    touched (the /admin paste-box outranks any machine sweep).

Wrong image beats no image NEVER; no image beats wrong image ALWAYS.
"""
import time

from gtin_secondpass import _token_matches

# ─── Verdicts (audit vocabulary, mirrors gtin_secondpass style) ──────────────
HAS_CATALOG = 'HAS-CATALOG'            # nothing to do, image already on spine
SKIPPED_OVERRIDE = 'SKIPPED-OVERRIDE'  # human curated image_thumb — terminal
NO_KEYS = 'NO-KEYS'                    # entry lacks brand/model/mpn to search
SEARCH_FAILED = 'SEARCH-FAILED'        # Browse search raised (transient)
NO_CANDIDATES = 'NO-CANDIDATES'        # no epid-associated candidates
NO_CATALOG_FOUND = 'NO-CATALOG-FOUND'  # candidates resolved, none carried one
RESCUED = 'RESCUED'                    # catalog image found + firewall passed

DEFAULT_MAX_RESOLVES = 3
DEFAULT_SLEEP_S = 0.5


def _entry_identity(entry):
    return (entry or {}).get('identity', {}) or {}


def _entry_epid(entry):
    ident = _entry_identity(entry)
    epid = ident.get('epid', '')
    if epid:
        return epid
    # substrate shape: demoted marketplace shadow
    return ((entry or {}).get('marketplace_ids', {}) or {}).get('ebay_epid', '')


def needs_rescue(entry):
    """True if this entry is missing a catalog image AND is machine-writable.

    The sweep's selection predicate, exported so the runner and tests share
    one definition. overrides.image_thumb is the human layer — terminal
    against machine writes, same doctrine as GTIN adjudication.
    """
    ident = _entry_identity(entry)
    if (ident.get('image_catalog') or '').strip():
        return False
    if ((entry or {}).get('overrides', {}) or {}).get('image_thumb'):
        return False
    return True


def rescue_catalog_image(slug, entry, *, ebay=None,
                         max_resolves=DEFAULT_MAX_RESOLVES,
                         sleep_s=DEFAULT_SLEEP_S):
    """Hunt the eBay catalog stock image for one spine entry.

    Pure evidence-gathering: performs NO spine writes (resolve_sku doctrine —
    the writer lives in skus_registry.set_image_catalog; the sweep runner
    composes the two). Returns:

      {'image_catalog': url_or_None,
       'image_provenance': receipt_or_None,
       'verdict': one of the module verdicts,
       'query': the search string used (None when skipped)}

    The receipt carries recovered_by/recovered_at + the winning candidate's
    item_id/epid/title, so a rescued image is always distinguishable from a
    resolve-time capture and traceable to its evidence.
    """
    if ebay is None:
        import ebay_api as ebay  # deferred: keeps module importable creds-free

    if not needs_rescue(entry):
        ident = _entry_identity(entry)
        verdict = (HAS_CATALOG if (ident.get('image_catalog') or '').strip()
                   else SKIPPED_OVERRIDE)
        return {'image_catalog': None, 'image_provenance': None,
                'verdict': verdict, 'query': None}

    ident = _entry_identity(entry)
    brand = ident.get('brand', '')
    mpn = ident.get('mpn', '')
    market_title = ident.get('market_title', '')

    # Query chain (spine vocabulary — there is no identity.model field):
    # brand+mpn is the precise form; the bound listing's market_title is the
    # broad form (long seller string, Browse handles it fine); the humanized
    # slug is the floor so a bare entry still gets a look.
    if brand and mpn:
        query = f'{brand} {mpn}'
    elif market_title:
        query = market_title
    elif brand or slug:
        query = f"{brand} {slug.replace('-', ' ')}".strip()
    else:
        return {'image_catalog': None, 'image_provenance': None,
                'verdict': NO_KEYS, 'query': None}

    model_token = mpn  # tokens gate on mpn only; no mpn -> epid-equality path

    try:
        cands = ebay.search_candidates(query, limit=10)
    except Exception as e:
        return {'image_catalog': None, 'image_provenance': None,
                'verdict': f'{SEARCH_FAILED}: {e}', 'query': query}

    # Catalog images live on catalog-associated listings only.
    assoc = [c for c in cands if c.get('epid')]
    if not assoc:
        return {'image_catalog': None, 'image_provenance': None,
                'verdict': NO_CANDIDATES, 'query': query}

    # Resolve-order ranking (the resolve budget is the scarce resource):
    #   0. the entry's own epid — strongest identity match available
    #   1. summary title already contains the mpn token — cheap positive signal
    #   2. everything else — seller titles routinely omit the MPN ('Sony a7 IV'
    #      not 'ILCE-7M4'), so these are NOT rejected here; the firewall runs
    #      AFTER resolve against getItem's identity.mpn (the gtin_secondpass
    #      lesson: summaries can't carry the evidence the gate needs).
    own_epid = _entry_epid(entry)

    def _rank(c):
        if own_epid and c.get('epid') == own_epid:
            return 0
        if model_token and _token_matches(c, model_token):
            return 1
        return 2

    assoc.sort(key=_rank)

    inspected = []
    resolves_spent = 0
    winner = None
    win_url = ''
    for c in assoc:
        if resolves_spent >= max_resolves:
            break
        if resolves_spent and sleep_s:
            time.sleep(sleep_s)
        resolves_spent += 1
        note = {'item_id': c.get('item_id'), 'epid': c.get('epid', ''),
                'title': c.get('title', '')}
        try:
            r = ebay.resolve(c['item_id'])
        except Exception as e:
            note['error'] = str(e)
            inspected.append(note)
            continue
        r_ident = (r.get('identity', {}) or {})
        url = (r_ident.get('image_catalog') or '').strip()
        note['had_catalog'] = bool(url)

        # Acceptance firewall (post-resolve, full evidence in hand). Either:
        #   (a) epid equality with the entry's own epid — identity-strongest;
        #   (b) the mpn token matches the summary title OR getItem's mpn.
        # No mpn and no epid match -> fail closed: a clean stock image of the
        # WRONG product must never win.
        epid_ok = bool(own_epid and c.get('epid') == own_epid)
        token_ok = bool(model_token and (
            _token_matches(c, model_token)
            or _token_matches({'title': '', 'mpn': r_ident.get('mpn', '')},
                              model_token)))
        note['accepted'] = bool(url and (epid_ok or token_ok))
        if url and not note['accepted']:
            note['rejected'] = 'identity-firewall'
        inspected.append(note)

        if note['accepted']:
            winner = {'item_id': c.get('item_id'), 'epid': c.get('epid', ''),
                      'title': c.get('title', ''), 'epid_match': epid_ok}
            win_url = url
            break

    if winner:
        receipt = {
            'recovered_by': 'image-second-pass',
            'recovered_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'query': query,
            'winner': {k: winner[k] for k in ('item_id', 'epid', 'title')},
            'epid_match': winner['epid_match'],
            'inspected': inspected,
        }
        return {'image_catalog': win_url, 'image_provenance': receipt,
                'verdict': RESCUED, 'query': query}

    return {'image_catalog': None, 'image_provenance': None,
            'verdict': NO_CATALOG_FOUND, 'query': query,
            'inspected': inspected}
