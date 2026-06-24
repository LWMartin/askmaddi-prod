"""slug_common.py — frozen slug primitives, faithful copies of the phantom-ops originals.

These two functions are the shared *definitions* the registry's slug layer
depends on. They are deliberately DUPLICATED from phantom-ops rather than
imported across the repo boundary, because slug_normalizer needs to live
in-repo (the gateway will eventually call it in a hot path for live card
creation — decision #3 of maddi-skus-registry), and a subprocess shell-out to a
phantom-ops checkout beside the repo is a liability in a request handler.

The duplication is SAFE because:
  1. Both functions are frozen by spec. `slugify()` is declared frozen in
     maddi-skus-registry ("rule changes are override-table ADDITIONS, NEVER
     slugify() edits"). `_norm()` is the alphanumeric-only normalization the
     whole join-check apparatus agrees on; changing it is never correct.
  2. Agreement is TESTED, not hoped for. test_slug_common_agreement.py asserts
     these produce byte-identical output to the phantom-ops originals across a
     corpus of slugs, skipping gracefully when phantom-ops is not beside the
     repo (mirroring test_contamination_bridge.py). Drift becomes a red CI run,
     not a silent Sigma-class join break.

Provenance (the originals these are copied from, verbatim logic):
  - slugify : phantom-ops claude/workspace/aggregator-build/ingest/adapters/_common.py
  - _norm   : phantom-ops claude/workspace/aggregator-build/registry_join_check.py

If you are tempted to edit either function: DON'T. Edit the override table
(slug minting) or the contamination_key bridge (join). These primitives are the
fixed point both repos rotate around — that is the entire reason they are
small, pure, and frozen.
"""
from __future__ import annotations

import re

# ── slugify ────────────────────────────────────────────────────────────────
# Faithful copy of phantom-ops ingest.adapters._common.slugify.
# Lowercase, collapse non-alphanumerics to single hyphens, cap length, never
# leave a trailing hyphen after truncation. Output constrained to [a-z0-9-].
SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int) -> str:
    """Lowercase, collapse non-alphanumerics to single hyphens, cap length.

    Output is constrained to ``[a-z0-9-]``. Truncation never leaves a trailing
    hyphen. Faithful copy of the phantom-ops original — FROZEN, never tuned.
    """
    slug = SLUG_STRIP_RE.sub("-", text.lower()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug


# ── _norm ──────────────────────────────────────────────────────────────────
# Faithful copy of phantom-ops registry_join_check._norm.
# Alphanumeric-only normalization that surfaces slug-convention near-matches
# (sony-a7iv ~ sony-a7-iv). The ONE definition of "same slug, different
# punctuation" that mint-time collision detection and after-the-fact join
# auditing both speak — kept identical to the phantom-ops audit engine by the
# agreement test.
def _norm(s: str) -> str:
    """Alphanumeric-only normalization. Faithful copy of the phantom-ops
    registry_join_check._norm — surfaces slug-convention near-matches
    (sony-a7iv ~ sony-a7-iv) so a collision reads as 'same product, slug
    differs' rather than 'missing'."""
    return re.sub(r"[^a-z0-9]", "", s.lower())
