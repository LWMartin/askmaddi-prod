"""
ebay_category_map.py — eBay categoryId -> controlled-vocab card category.
=========================================================================
The minting wire's part (e). A demand-discovered (minted) slug has no registry
entry to read `category` from, so on the mint path the category is DERIVED from
the eBay item's `ebay_category_id` — the locked "marketplace truth, no
hand-tagging" decision (2026-06-29-comparator-complete-factory-scoped). This
module is that derivation, and ONLY that: it does not fetch, write, or judge
listings; it maps one integer-ish id string to one of the controlled buckets.

CONTROLLED VOCAB (authoritative, from demand_log + the live card corpus):
    'body'    — camera bodies
    'lens'    — lenses (the registry stores the COARSE bucket; lens/{prime,zoom,
                macro,cine} sub-typing is a card-BUILD concern, not a registry
                category — the frozen sigma-35-art-dg-dn-ii entry carries bare
                'lens', proving the coarse level is what skus.json holds)
    'support' — tripods, heads, rigs, gimbals, mounts

SEED MAPPING is grounded in the four frozen registry entries, which are a
hand-verified labeled table (each entry pairs an ebay_category_id with the
human-assigned category):
    30093 -> support   (Peak Design Pro Tripod, Travel Tripod)
    3323  -> lens       (Sigma 35 Art)
    88433 -> body       (Sony A7 IV)
Extended with the stable eBay leaf categories in the camera-gear tree. eBay
category ids are durable (they change rarely and announce changes), so a static
table is correct here; a new/unmapped id is not an error but a REVIEW signal.

UNKNOWN-ID DISCIPLINE (the safety seam): an id with no mapping returns '' (empty
category), NOT a guess. On the mint path an empty category keeps the entry at
minted_needs_review=True, so it lands under Lee's eyes at the publish gate with
the category blank — exactly the "look harder" surface the air-gap review is for.
Guessing a bucket would risk a mis-bucketed card slipping through; abstaining
routes the judgment to the human, mirroring the resolver's confidence-floor and
the comparator typer's abstain discipline.
"""

# eBay leaf categoryId -> controlled bucket. Seeded from the frozen registry
# entries (hand-verified) + the stable camera-gear leaves. Keys are strings
# because ebay_category_id is captured as a string ('categoryId' is a string in
# the getItem payload).
_CATEGORY_MAP = {
    # ── bodies ──
    '88433': 'body',     # Digital Cameras (the Sony A7 IV's leaf — frozen entry)
    '31388': 'body',     # Digital Cameras (alt leaf seen in the tree)
    '15230': 'body',     # Film Cameras (still a body)
    # ── lenses ──
    '3323': 'lens',      # Camera Lenses (the Sigma 35 Art's leaf — frozen entry)
    '78997': 'lens',     # Lenses & Filters > Lenses (alt leaf)
    '64352': 'lens',     # Lenses for interchangeable-lens cameras
    # ── support ──
    '30093': 'support',  # Tripods & Supports (Peak Design tripods — frozen entry)
    '30097': 'support',  # Other Camera Tripods & Supports — sibling leaf of 30093
                         # in eBay's own Tripods & Supports family (Tripods &
                         # Monopods / Other Tripods & Supports / Stabilizers /
                         # Tripod Heads). Added 2026-07-29 after ulanzi-f38-zero
                         # was rebound onto it (2026-07-09) and abstained here
                         # while its facet still read 'support' from mint-time
                         # leaf 30093. Verified against the live browse tree, not
                         # inferred from the entry that needed it.
    '177853': 'support', # Tripod Heads
    '163418': 'support', # Stabilizers & Gimbals
    '30090': 'support',  # Monopods
    # ── action cameras (video vertical) ──
    '11724': 'action_cam', # Camcorders — where GoPro/action cams live on eBay.
                         # The video vertical (added 2026-08) sets facet from
                         # vertical context; mapping the precise camcorder leaf
                         # lets the mint-path derivation agree. Generic Digital
                         # Cameras (31388) stays 'body': an action cam listed
                         # there is an id imprecision, which the guard reports as
                         # a NON-BREAKING notice (facet outside the settled
                         # body/lens/support regime), not a hard drift.
}

# The controlled buckets this module is allowed to emit (besides '' = unknown).
# body/lens/support are the SETTLED regime the eBay id authoritatively derives;
# action_cam (and future vertical buckets) are derived by the vertical pipeline,
# so the map is advisory there — the guard (test) hard-enforces only the settled
# regime and reports vertical/unmapped cases as loud, non-breaking notices.
CONTROLLED_CATEGORIES = ('body', 'lens', 'support', 'action_cam')

# The regime whose facets the eBay id AUTHORITATIVELY derives. A genuine
# disagreement WITHIN this set is a hard drift (the guard fails). A facet outside
# it (a vertical bucket) or an unmapped id is a review notice, never a hard fail.
SETTLED_FACETS = ('body', 'lens', 'support')


def category_for(ebay_category_id):
    """Map an eBay categoryId to a controlled card category, or '' if unmapped.

    Parameters
    ----------
    ebay_category_id : str | int | None
        The id from resolved['identity']['ebay_category_id']. Tolerant of int
        (coerced to str) and of None/'' (-> '', the unknown signal).

    Returns
    -------
    str
        One of CONTROLLED_CATEGORIES, or '' when the id is unknown/blank. '' is
        the deliberate REVIEW signal on the mint path — never a guessed bucket.
    """
    if ebay_category_id is None:
        return ''
    key = str(ebay_category_id).strip()
    if not key:
        return ''
    return _CATEGORY_MAP.get(key, '')


def is_known(ebay_category_id):
    """True iff the id maps to a controlled bucket (i.e. category_for != '').

    Lets a caller distinguish 'mapped to a real bucket' from 'unknown -> review'
    without re-deriving the empty-string sentinel.
    """
    return category_for(ebay_category_id) != ''


def reconcile(facet, ebay_category_id):
    """Classify a registry (facet, ebay_category_id) pair for the drift guard.

    The one definition of 'does the map agree with the registry', shared by the
    guard test so its behaviour is unit-testable in isolation (not only via the
    live spine). Returns one of:

      'ok'     — map and facet agree (or the facet IS what the id maps to).
      'drift'  — BOTH are settled body/lens/support facets and they disagree.
                 This is the genuine seed-table drift the guard hard-fails on
                 (the 30093->30097 leaf-rebind class).
      'notice' — the id is unmapped ('' ), OR the facet is a vertical bucket the
                 map does not own (action_cam and future gimbal/drone/...). The
                 map has no jurisdiction here, so this is a NON-BREAKING review
                 nudge, never a hard failure — the generalisation that stops a
                 new product category from jamming the bank.
    """
    mapped = category_for(ebay_category_id)
    if mapped == (facet or ''):
        return 'ok'
    if mapped in SETTLED_FACETS and facet in SETTLED_FACETS:
        return 'drift'
    return 'notice'
