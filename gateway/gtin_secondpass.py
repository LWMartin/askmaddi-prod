"""
gtin_secondpass — eBay second-pass GTIN recovery + admission gate (substrate step 5b).
========================================================================
Fills the NO-GTIN tail: used / one-of-a-kind listings whose seller didn't
catalog-associate, so L1 (`gtin_extract` inside `_extract_identity`) found no
GTIN on THAT listing. The same physical product almost always exists as a
catalog-associated (ePID-bearing) listing elsewhere on eBay carrying the GTIN.
We already hold brand+mpn, so L2 is a SECOND PASS AGAINST EBAY — our own
entitled access, not an external source (Wikidata/GS1 are keyed GTIN->product,
the wrong direction; path retired, see substrate Amendment A).

ARCHITECTURE (why this is NOT inside _extract_identity)
--------------------------------------------------------
All three mint paths (tap/capture -> review_queue.promote, factory
resolve_pass -> resolve_proposal, backfill_skus) converge on
ebay_api.resolve() -> _extract_identity() -> skus_registry.build_entry().
L2 cannot live inside that seam:
  1. RECURSION — recover_gtin() calls resolve() per candidate; resolve()
     calls _extract_identity; L2 inside it would trigger second-passes
     from second-passes.
  2. LATENCY — the tap path is interactive; ~5 resolves + politeness
     sleeps inside a browser endpoint is wrong.
  3. FROZEN IDENTITY — review_queue.promote() deliberately builds from the
     frozen capture; fresh network at build-time violates that contract.
Instead L2 runs as a REGISTRY-LEVEL sweep (tools/secondpass_gtin.py) over
entries where identity.gtin is null — one mechanism heals every path,
including future express mints, with zero write-path surgery. Writes go
through skus_registry.set_gtin (upgrade-only; upsert would 'unchanged'-skip
a gtin-only change since gtin is not an identity idempotency key).

THE ADMISSION GATE (Amendment A, mint-time, Axis A — ALL four must hold)
------------------------------------------------------------------------
  1. CORROBORATION  >=2 catalog-associated candidates carry a GTIN.
                    (Kills the single-candidate a7CR class — one listing
                    cannot self-certify; the conflict check needs >=2
                    candidates to detect a wrong-product match.)
  2. AGREEMENT      those candidates agree on exactly ONE GTIN.
                    (Disagreement = CONFLICT, never silent-pick — no
                    majority vote, 2-vs-1 still drops.)
  3. SOURCE         the winning GTIN comes from product.gtins (catalog) on
                    >=1 agreeing candidate, not aspect-only (seller
                    free-text is format-validated but not product-matched).
  4. IDENTITY MATCH >=1 agreeing candidate's title/mpn contains the SKU's
                    model token. (Makes "agreement" MEAN "same right
                    product" — two a7CR listings would agree and pass 1–3.)
Else GTIN stays null. Precision-biased, high-drop, BY DESIGN: the drop is
the feature. A dropped SKU is not card-killed — it falls to the substrate's
Gemma+title fallback identity; card-worthiness is gate 2 (card-time), a
separate stage.

Pure logic (admission_gate) is separated from network (recover_gtin) so the
gate is offline-fixture-testable against the 7 probed SKU verdicts.
"""
import re
import time


# Verdicts, in clause order. SEARCH/KEYS failures are wire-level, not gate-level.
ADMIT = "ADMIT"
NO_GTIN_DROP = "NO-GTIN-DROP"                  # clause 1: zero gtin-bearing candidates
SINGLE_CANDIDATE_DROP = "SINGLE-CANDIDATE-DROP"  # clause 1: exactly one (a7CR class)
CONFLICT_DROP = "CONFLICT-DROP"                # clause 2: >1 distinct GTIN
SOURCE_DROP = "SOURCE-DROP"                    # clause 3: aspect-only, no catalog source
TOKEN_DROP = "TOKEN-DROP"                      # clause 4: no candidate matches model token
NO_CANDIDATES = "NO-CANDIDATES"                # wire: no catalog-assoc results at all
NO_KEYS = "NO-KEYS"                            # wire: SKU lacks brand and mpn/model
SEARCH_FAILED = "SEARCH-FAILED"                # wire: Browse search raised

# Own-listing (L1-first) verdicts — the step that runs BEFORE the second pass.
OWN_LISTING_L1 = "OWN-LISTING-L1"              # own payload carries GTIN: true L1
OWN_LISTING_BARE = "OWN-LISTING-BARE"          # own payload has no GTIN -> second pass
OWN_LISTING_DEAD = "OWN-LISTING-DEAD"          # listing ended/gone -> second pass
NO_LEGACY_ID = "NO-LEGACY-ID"                  # entry lacks legacy_item_id -> second pass


def recover_own_listing(legacy_item_id, *, ebay=None):
    """L1-first step: re-resolve the SKU's OWN listing and read its L1 GTIN.

    WHY THIS EXISTS (provenance inversion, found on the first live sweep):
    L1 extraction only fires at mint-time, so every entry minted BEFORE
    substrate-5 landed has a null gtin even when its own listing's payload
    carries one (the 2026-06-30 probe measured 7/14 such). Recovering those
    via the second-pass search would stamp 'secondpass:' provenance on SKUs
    whose strictly-stronger first-pass GTIN is one resolve() away — and would
    consult the admission gate (built for cross-listing corroboration) where
    no corroboration is needed. Own listing first; search only when the own
    payload is genuinely barren.

    The returned provenance is the UNMODIFIED L1 shape from _extract_identity
    (same listing, same extraction, same trust as a mint-time L1), plus an
    additive audit key `recovered_by: 'sweep-own-listing'` + timestamp so a
    backfilled L1 is always distinguishable from a minted one without
    inventing a third trust tier. An L1-internal conflict flag persists as-is
    — identical to mint behavior, no special-casing.

    A dead listing (used goods sell; resolve raises) is an expected path,
    not an error — verdict OWN-LISTING-DEAD, caller falls through to the
    second pass, which exists for exactly that case.

    Returns {'gtin', 'gtin_provenance', 'verdict'}.
    """
    if ebay is None:
        import ebay_api as ebay  # deferred: keeps module importable creds-free

    if not legacy_item_id:
        return {'gtin': None, 'gtin_provenance': None, 'verdict': NO_LEGACY_ID}

    item_id = f'v1|{legacy_item_id}|0'  # same reconstruction as the L1 probe
    try:
        r = ebay.resolve(item_id)
    except Exception as e:
        return {'gtin': None, 'gtin_provenance': None,
                'verdict': f'{OWN_LISTING_DEAD}: {e}'}

    idn = r.get('identity', {}) or {}
    gtin = idn.get('gtin')
    if not gtin:
        return {'gtin': None, 'gtin_provenance': None, 'verdict': OWN_LISTING_BARE}

    prov = dict(idn.get('gtin_provenance') or {})
    prov['recovered_by'] = 'sweep-own-listing'
    prov['recovered_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    return {'gtin': gtin, 'gtin_provenance': prov, 'verdict': OWN_LISTING_L1}


def _norm(s):
    """Casefold + strip non-alphanumerics for containment matching.

    'ILCE-7M4' -> 'ilce7m4', matched inside 'Sony Alpha a7 IV ... ILCE7M4'.
    Sellers vary hyphen/space/case freely in titles; the token itself is the
    invariant. Deliberately NOT fuzzy beyond this — clause 4 is the wrong-
    product firewall and must stay strict.
    """
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def _token_matches(candidate, model_token):
    tok = _norm(model_token)
    if not tok:
        return False  # fail-closed: no token, no clause-4 pass (precision bias)
    return tok in _norm(candidate.get('title')) or tok in _norm(candidate.get('mpn'))


def admission_gate(candidates, model_token):
    """Apply the 4-clause GTIN admission gate. PURE — no network, no I/O.

    Args:
      candidates:  resolved catalog-associated candidates, each a dict:
                   {item_id, epid, title, mpn, gtin, chosen_source}
                   (gtin is the L1-extracted canonical GTIN-14 or None;
                   chosen_source is gtin_extract's per-candidate source).
                   Candidates whose resolve errored should carry 'error' and
                   no gtin — they count toward nothing.
      model_token: the SKU's model token (mpn, e.g. 'ILCE-7M4'; caller falls
                   back to model when mpn is absent).

    Returns (gtin_or_None, verdict, receipt). The receipt records every
    clause's evidence so a drop is auditable and a future /admin view can
    show WHY without re-running the wire.
    """
    bearing = [c for c in candidates if c.get('gtin')]
    distinct = sorted({c['gtin'] for c in bearing})

    receipt = {
        'n_candidates': len(candidates),
        'n_gtin_bearing': len(bearing),
        'distinct_gtins': distinct,
        'model_token': model_token or '',
        'candidates': [
            {
                'item_id': c.get('item_id', ''),
                'epid': c.get('epid', ''),
                'gtin': c.get('gtin'),
                'chosen_source': c.get('chosen_source'),
                'title': (c.get('title') or '')[:70],
                'token_match': _token_matches(c, model_token),
                **({'error': c['error']} if c.get('error') else {}),
            }
            for c in candidates
        ],
    }

    # Clause 1 — corroboration
    if len(bearing) == 0:
        return None, NO_GTIN_DROP, receipt
    if len(bearing) == 1:
        return None, SINGLE_CANDIDATE_DROP, receipt

    # Clause 2 — agreement (exactly one distinct GTIN; never silent-pick)
    if len(distinct) != 1:
        return None, CONFLICT_DROP, receipt

    # Clause 3 — authoritative source on >=1 agreeing candidate
    if not any(c.get('chosen_source') == 'product.gtins' for c in bearing):
        return None, SOURCE_DROP, receipt

    # Clause 4 — product-identity match on >=1 agreeing candidate
    if not any(_token_matches(c, model_token) for c in bearing):
        return None, TOKEN_DROP, receipt

    return distinct[0], ADMIT, receipt


def _provenance(verdict, receipt, query, gtin):
    """Assemble the identity.gtin_provenance block for a second-pass outcome.

    Superset-compatible with L1's shape (chosen_source / conflict /
    observations) so the Axis A migration and /admin treat both uniformly.
    chosen_source is namespaced 'secondpass:product.gtins' — a recovered GTIN
    is never mistakable for a first-pass one.
    """
    return {
        'chosen_source': 'secondpass:product.gtins' if gtin else None,
        'conflict': verdict == CONFLICT_DROP,
        'observations': [
            {
                'source': 'secondpass:candidate',
                'identifier_type': 'GTIN',
                'raw': c['gtin'],
                'gtin14': c['gtin'],
                'valid': True,
                'item_id': c['item_id'],
                'epid': c['epid'],
            }
            for c in receipt['candidates'] if c.get('gtin')
        ],
        'recovery': {
            'method': 'ebay-secondpass',
            'query': query,
            'verdict': verdict,
            'recovered_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            **receipt,
        },
    }


def recover_gtin(brand, mpn, model=None, *, ebay=None,
                 max_resolves=5, sleep_s=0.5):
    """The L2 wire: search brand+mpn -> catalog-assoc candidates -> gate.

    Composition of existing, proven functions (Amendment A build implication):
    ebay.search_candidates -> filter ePID-bearing -> ebay.resolve each
    (capped at max_resolves, politeness sleep between live calls) -> the
    4-clause admission_gate -> admit or null.

    Args:
      brand, mpn, model:  the SKU's identity keys. Query prefers brand+mpn
                          (strongest); falls back to brand+model. model_token
                          for clause 4 is mpn, falling back to model.
      ebay:               injected module/obj exposing .search_candidates and
                          .resolve (production: ebay_api; tests: fake). Kept
                          injected — same DI pattern as resolve_proposal.
      max_resolves:       per-SKU resolve cap (API politeness; probe used 5).
      sleep_s:            inter-resolve sleep. Tests pass 0.

    Returns {'gtin', 'gtin_provenance', 'verdict', 'query'} — the same
    identity.{gtin, gtin_provenance} shape L1 writes, plus wire metadata for
    the sweep's report. Per-candidate resolve errors are recorded in the
    receipt and do not abort the SKU (probe behavior).
    """
    if ebay is None:
        import ebay_api as ebay  # deferred: keeps module importable creds-free

    brand = (brand or '').strip()
    mpn = (mpn or '').strip()
    model = (model or '').strip()

    if brand and mpn:
        query = f'{brand} {mpn}'
    elif brand and model:
        query = f'{brand} {model}'
    else:
        return {'gtin': None, 'gtin_provenance': None,
                'verdict': NO_KEYS, 'query': None}

    model_token = mpn or model

    try:
        cands = ebay.search_candidates(query, limit=10)
    except Exception as e:
        return {'gtin': None, 'gtin_provenance': None,
                'verdict': f'{SEARCH_FAILED}: {e}', 'query': query}

    # Only catalog-associated candidates can carry a catalog GTIN.
    assoc = [c for c in cands if c.get('epid')]
    if not assoc:
        return {'gtin': None, 'gtin_provenance': None,
                'verdict': NO_CANDIDATES, 'query': query}

    resolved = []
    for i, c in enumerate(assoc[:max_resolves]):
        try:
            r = ebay.resolve(c['item_id'])
            idn = r.get('identity', {}) or {}
            resolved.append({
                'item_id': c['item_id'],
                'epid': c.get('epid', ''),
                'title': c.get('title', ''),
                'mpn': idn.get('mpn', ''),
                'gtin': idn.get('gtin'),
                'chosen_source': (idn.get('gtin_provenance') or {}).get('chosen_source'),
            })
        except Exception as e:
            resolved.append({'item_id': c['item_id'], 'epid': c.get('epid', ''),
                             'title': c.get('title', ''), 'error': str(e)})
        if sleep_s and i + 1 < min(len(assoc), max_resolves):
            time.sleep(sleep_s)

    gtin, verdict, receipt = admission_gate(resolved, model_token)
    return {
        'gtin': gtin,
        'gtin_provenance': _provenance(verdict, receipt, query, gtin),
        'verdict': verdict,
        'query': query,
    }
