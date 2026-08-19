"""Resolve identity from manufacturer/Shopify surfaces (rung C).

Extracts product identity from the manufacturer's product page by reusing the
existing fact_pipeline.remix_metafield extractors for Hydrogen pages, and
falling back to JSON-LD Product schema for non-Hydrogen pages.
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Callable, Optional

from remix_metafield import extract_remix_context, find_root_product


def resolve(
    target: dict,
    *,
    fetch: Optional[Callable[[str], str]] = None,
) -> Optional[dict]:
    """Resolve product identity from manufacturer/Shopify surface.

    Args:
        target: Dict with optional 'source_url' key (and vendor, model for context).
        fetch: Callable(url) -> html. If None, uses urllib.request.urlopen.

    Returns:
        SourceResolution dict with source, identity, confidence, deterministic,
        aliases, relations, raw, why. None if no source_url or parsing fails.
    """
    source_url = target.get("source_url")
    if not source_url:
        return None

    if fetch is None:
        fetch = _default_fetch

    try:
        html = fetch(source_url)
    except Exception:
        return None

    # Try Hydrogen/Remix extraction first
    result = _try_hydrogen_extraction(html, target)
    if result:
        return result

    # Fall back to JSON-LD Product schema
    result = _try_jsonld_extraction(html, target)
    if result:
        return result

    return None


def _default_fetch(url: str) -> str:
    """Default fetch implementation using urllib."""
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read().decode("utf-8", errors="ignore")


def _try_hydrogen_extraction(html: str, target: dict) -> Optional[dict]:
    """Extract from Hydrogen/Remix page."""
    ctx = extract_remix_context(html)
    if ctx is None:
        return None

    root_product = find_root_product(ctx)
    if root_product is None:
        return None

    # Extract gtin and mpn from variants
    gtin = None
    mpn = None
    variants = root_product.get("variants", {})
    if isinstance(variants, dict):
        nodes = variants.get("nodes", [])
        if nodes and isinstance(nodes[0], dict):
            gtin = nodes[0].get("barcode")
            mpn = nodes[0].get("sku")

    # Extract brand
    brand = root_product.get("vendor")

    # Extract title and handle
    title = root_product.get("title")
    handle = root_product.get("handle")

    # Extract image
    image = None
    featured = root_product.get("featuredImage")
    if isinstance(featured, dict):
        image = featured.get("url")

    # Build canonical_model from title
    canonical_model = title if title else None

    # Build aliases from product name variants, excluding spec field names
    aliases = []
    if title:
        aliases.append(title)
    if handle:
        aliases.append(handle)
    if brand and canonical_model:
        # vendor+model format
        aliases.append(f"{brand} {canonical_model}")

    # Remove duplicates while preserving order
    seen = set()
    unique_aliases = []
    for alias in aliases:
        if alias not in seen:
            seen.add(alias)
            unique_aliases.append(alias)

    # Check if we have at least some identity
    if not (gtin or mpn or brand or canonical_model or image):
        return None

    deterministic = bool(gtin or mpn)
    confidence = 1.0 if deterministic else 0.5

    identity = {
        "gtin": gtin,
        "mpn": mpn,
        "brand": brand,
        "canonical_model": canonical_model,
        "image": image,
    }

    return {
        "source": "mfr_surface",
        "identity": identity,
        "confidence": confidence,
        "deterministic": deterministic,
        "aliases": unique_aliases,
        "relations": {"predecessor": [], "competitor": []},
        "raw": {"root_product": root_product},
        "why": "Hydrogen/Remix metafield extraction",
    }


def _try_jsonld_extraction(html: str, target: dict) -> Optional[dict]:
    """Extract from JSON-LD Product schema."""
    # Find and parse JSON-LD script tag
    pattern = r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>'
    matches = re.findall(pattern, html, re.DOTALL | re.IGNORECASE)

    if not matches:
        return None

    for match in matches:
        try:
            data = json.loads(match)
        except (ValueError, TypeError):
            continue

        # Check if this is a Product schema
        if data.get("@type") != "Product":
            continue

        # Extract fields
        gtin = data.get("gtin13")
        mpn = data.get("mpn")
        brand_obj = data.get("brand")
        brand = None
        if isinstance(brand_obj, dict):
            brand = brand_obj.get("name")
        elif isinstance(brand_obj, str):
            brand = brand_obj

        name = data.get("name")
        image = data.get("image")

        # Build canonical_model from name
        canonical_model = name if name else None

        # Build aliases
        aliases = []
        if name:
            aliases.append(name)
        if brand and canonical_model:
            aliases.append(f"{brand} {canonical_model}")

        # Remove duplicates
        seen = set()
        unique_aliases = []
        for alias in aliases:
            if alias not in seen:
                seen.add(alias)
                unique_aliases.append(alias)

        # Check if we have at least some identity
        if not (gtin or mpn or brand or canonical_model or image):
            continue

        deterministic = bool(gtin or mpn)
        confidence = 1.0 if deterministic else 0.5

        identity = {
            "gtin": gtin,
            "mpn": mpn,
            "brand": brand,
            "canonical_model": canonical_model,
            "image": image,
        }

        return {
            "source": "mfr_surface",
            "identity": identity,
            "confidence": confidence,
            "deterministic": deterministic,
            "aliases": unique_aliases,
            "relations": {"predecessor": [], "competitor": []},
            "raw": {"jsonld": data},
            "why": "JSON-LD Product schema extraction",
        }

    return None
