"""identity_lookup binding for Lane A Rung 0 — the spine<->search seam.

Builds a resolver `(sidecar) -> identity_key | None` over the CARDED spine
(skus.json). v1 deliberately resolves against the curated card registry, NOT the
229k-row Adorama catalog: the catalog contains accessories, so "exists in the
catalog" is not a canonical-vs-accessory signal. A result that resolves to a
carded product is guaranteed-canonical (Rung 0); everything else falls to the
cheap accessory-marker check and then (bounded) Qwen3 arbitration downstream.

Keyed by GTIN, then MPN, then a normalized brand+model string — the same
identity anchors skus_registry exposes. Broadening the resolver to a fuller
body/lens registry is the precision upgrade tracked in the seed.
"""
from __future__ import annotations

import re

import skus_registry

_NORM_RE = re.compile(r"[^a-z0-9]+")


def _norm(text):
    return _NORM_RE.sub(" ", (text or "").lower()).strip()


def build_identity_lookup(skus_path=None):
    """Return a pure `(sidecar) -> identity_key|None` closure over the spine."""
    registry = skus_registry.load_registry(skus_path) if skus_path \
        else skus_registry.load_registry()
    skus = registry.get("skus", {}) if isinstance(registry, dict) else {}

    by_gtin, by_mpn, by_bm = {}, {}, {}
    for slug, entry in skus.items():
        if not isinstance(entry, dict):
            continue
        gtin = skus_registry.get_gtin(entry)
        if gtin:
            by_gtin[str(gtin).strip()] = slug
        mpn = (entry.get("identity") or {}).get("mpn") or entry.get("mpn")
        if mpn:
            by_mpn[_norm(mpn)] = slug
        vendor = entry.get("vendor") or entry.get("brand") or ""
        model = entry.get("model") or ""
        bm = _norm(f"{vendor} {model}")
        if bm:
            by_bm[bm] = slug

    def lookup(sidecar):
        if not sidecar:
            return None
        gtin = sidecar.get("gtin")
        if gtin and str(gtin).strip() in by_gtin:
            return by_gtin[str(gtin).strip()]
        mpn = sidecar.get("mpn")
        if mpn and _norm(mpn) in by_mpn:
            return by_mpn[_norm(mpn)]
        bm = _norm(f"{sidecar.get('brand','')} {sidecar.get('model','')}")
        if bm and bm in by_bm:
            return by_bm[bm]
        return None

    return lookup
