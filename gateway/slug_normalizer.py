#!/usr/bin/env python3
"""slug_normalizer.py — canonical registry-slug resolver for NEW SKUs.

Build step 2 of maddi-skus-registry (decision LOCKED 2026-06-15). This is the
ONE sanctioned place a new card's `sku_id` slug is minted. Its entire reason to
exist is to protect the frozen-slug invariant that registry_join_check.py polices
after the fact:

  - Existing/cadre slugs are FROZEN FACTS. Read, never regenerate. The seed cadre
    (peak-design-pro-tripod, peak-design-travel-tripod, sigma-35-art-dg-dn-ii,
    sony-a7iv) are the first override-table entries — hand-curated by design, not
    grandfathered exceptions.
  - The generated tail is FROZEN, never tuned. We reuse the already-tested
    `slugify()` UNCHANGED. Per the spec: rule changes are override-table
    ADDITIONS, never slugify() edits — because every slugify edit silently
    re-slugs all future cards and re-opens the drift this closes.
  - A deterministic rule provably can't match human taste:
    slugify("Sigma 35mm f/1.4 Art", 60) -> "sigma-35mm-f-1-4-art", NOT the
    hand-authored "sigma-35-art-dg-dn-ii". So the override table is the only
    sanctioned deviation, and a freshly generated slug is a PROPOSAL a human
    confirms (and usually overrides) before it is frozen on creation.

GRADUATED INTO askmaddi-prod (2026-06-24). Previously lived in phantom-ops and
imported `ingest.adapters._common.slugify` + `registry_join_check._norm` across
the repo boundary. It now imports both from the in-repo `slug_common` module —
frozen, faithful copies guarded by test_slug_common_agreement.py. This is the
move decision #3 anticipated: the gateway will eventually call resolve_slug() in
a hot path for live card creation, and a cross-repo import (or subprocess
shell-out to a phantom-ops checkout) is a liability in a request handler.

What this module does NOT do:
  - It does not invent contamination keys. The skus.json -> contamination.json
    bridge is the explicit `contamination_key` field, hand-authored, and may
    legitimately differ from the slug (sony-a7iv registry slug bridges to the
    sony-a7-iv contamination key). registry_join_check.py is the teeth there.
  - It does not write anything. It resolves a slug and reports how it got there;
    the registry writer freezes it.
  - It never edits slugify(). If you are tempted to, add an override instead.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

# Reuse the frozen, tested generator and the join-check normalization rather
# than re-deriving either — agreeing with both is the point. These are the
# in-repo faithful copies (slug_common), kept identical to the phantom-ops
# originals by the agreement test.
from slug_common import slugify, _norm

_THIS = Path(__file__).resolve().parent
# The live spine: askmaddi-prod data/skus.json, beside data/cards/.
_SKUS = _THIS.parent / "data" / "skus.json"

# Same length cap the cadre slugs and adapters use. slugify() guarantees no
# trailing hyphen after truncation.
_SLUG_MAX = 60


@dataclass
class SlugResolution:
    """How a slug was decided, surfaced for human review before freeze."""
    slug: str
    source: str           # 'override' | 'generated'
    input_text: str       # the vendor+model text the generator saw
    needs_review: bool    # True for 'generated' — a proposal, not a fact
    collision: str | None = None   # existing slug this normalizes the same as

    def line(self) -> str:
        tag = self.source.upper()
        base = f"[{tag}] {self.slug}"
        if self.source == "generated":
            base += f"  (from {self.input_text!r} — REVIEW before freeze)"
        if self.collision:
            base += f"  \u26a0 normalizes same as existing '{self.collision}'"
        return base


def _identity_key(vendor: str, model: str) -> str:
    """Normalized identity for reverse-lookup: 'Sony' + 'A7 IV (ILCE-7M4)' and
    'sony-a7iv' must both reduce to the same key so an existing card's
    vendor/model resolves to its FROZEN slug instead of minting a fresh one.
    Reuses the join-check normalization so there is one definition of 'same'.
    """
    return _norm(f"{vendor} {model}")


def _load_override_table(skus_path: Path) -> tuple[set[str], dict[str, str]]:
    """The override table = every entry already in the registry.

    Returns (frozen_slugs, identity_to_slug) where identity_to_slug maps a
    normalized vendor+model identity to its frozen slug. Tolerates the shapes
    seen in the tree:
      - {... "skus": {<slug>: {vendor,model,...}}}  (askmaddi-prod data/skus.json)
      - {... <slug>: {...}}  /  [ {...} ]            (older fixtures variants)

    A slug present here is a FROZEN FACT. The identity index is what protects
    the hand-authored cadre slugs (sony-a7iv, sigma-35-art-dg-dn-ii) whose
    mechanical slug would NOT equal the frozen one — the exact rebuild-fracture
    the spec warns about.
    """
    if not skus_path.exists():
        return set(), {}

    data = json.loads(skus_path.read_text())

    if isinstance(data, dict) and isinstance(data.get("skus"), dict):
        entries = data["skus"]
    elif isinstance(data, dict):
        entries = {k: v for k, v in data.items() if not k.startswith("_")}
    elif isinstance(data, list):
        entries = {}
        for e in data:
            sid = (e or {}).get("sku_id") or (e or {}).get("slug")
            if sid:
                entries[sid] = e
    else:
        return set(), {}

    slugs: set[str] = set()
    by_identity: dict[str, str] = {}
    for slug, e in entries.items():
        slugs.add(slug)
        e = e or {}
        vendor = e.get("vendor", "")
        model = e.get("model", "")
        if vendor or model:
            by_identity[_identity_key(vendor, model)] = slug
    return slugs, by_identity


def resolve_slug(
    vendor: str,
    model: str,
    *,
    override: str | None = None,
    skus_path: Path = _SKUS,
) -> SlugResolution:
    """Resolve the canonical registry slug for one SKU.

    Resolution order (the locked decision, concretely):
      1. Explicit `override` — the hand-authored slug a human chose for this card.
         This is the sanctioned deviation. Returned verbatim, no review flag.
      2. Frozen by IDENTITY — if this vendor+model already has a registry entry,
         return its frozen slug. This protects hand-authored cadre slugs whose
         mechanical form differs (sony-a7iv, sigma-35-art-dg-dn-ii): a Stage-6
         rebuild feeding vendor/model gets the FROZEN slug, never a fresh mint.
      3. Frozen by SLUG — the generated tail already exists verbatim; read it.
      4. Generated tail — slugify(vendor + " " + model), UNCHANGED rule. Flagged
         needs_review=True: a proposal, because the rule can't match human taste.

    Collision note: if a generated slug normalizes (alphanumeric-only) to the
    same key as an existing-but-textually-different slug, that is surfaced — it
    is exactly the sony-a7iv ~ sony-a7-iv class of silent-join-break that
    registry_join_check.py exists to catch. We catch it at mint time instead.
    """
    frozen, by_identity = _load_override_table(skus_path)
    text = f"{vendor} {model}".strip()

    # 1. Explicit human override wins and is authoritative — but if it collides
    #    with an existing DIFFERENT slug under normalization, say so loudly.
    if override:
        coll = _collision(override, frozen)
        return SlugResolution(
            slug=override, source="override", input_text=text,
            needs_review=False,
            collision=coll if coll and coll != override else None,
        )

    # 2. Frozen by identity — the cadre-protecting lookup. An existing card's
    #    vendor/model resolves to its frozen slug even when that slug was
    #    hand-authored away from the mechanical form.
    ident = _identity_key(vendor, model)
    if ident in by_identity:
        return SlugResolution(
            slug=by_identity[ident], source="override", input_text=text,
            needs_review=False, collision=None,
        )

    generated = slugify(text, _SLUG_MAX)

    # 3. Frozen by slug — generated form already exists verbatim; read as a fact.
    if generated in frozen:
        return SlugResolution(
            slug=generated, source="override", input_text=text,
            needs_review=False, collision=None,
        )

    # 4. Genuinely new -> generated proposal, must be reviewed before freeze.
    coll = _collision(generated, frozen)
    return SlugResolution(
        slug=generated, source="generated", input_text=text,
        needs_review=True,
        collision=coll if coll and coll != generated else None,
    )


def _collision(candidate: str, frozen: set[str]) -> str | None:
    """Return an existing slug that normalizes the same as `candidate`, if any.

    Uses the shared _norm so mint-time collision detection and after-the-fact
    join auditing speak the same normalization — no second, drifting definition
    of 'same slug, different punctuation'.
    """
    cn = _norm(candidate)
    for f in frozen:
        if f != candidate and _norm(f) == cn:
            return f
    return None


def _main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Resolve a canonical registry slug for a new SKU "
                    "(override-table-first; generated slugs are proposals).")
    ap.add_argument("vendor")
    ap.add_argument("model")
    ap.add_argument("--override", help="hand-authored slug (the sanctioned deviation)")
    ap.add_argument("--skus", type=Path, default=_SKUS,
                    help="path to skus.json (the override table)")
    args = ap.parse_args(argv)

    res = resolve_slug(args.vendor, args.model,
                       override=args.override, skus_path=args.skus)
    print(res.line())
    # Non-zero exit when a human MUST look: a generated proposal or any collision.
    return 1 if (res.needs_review or res.collision) else 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
