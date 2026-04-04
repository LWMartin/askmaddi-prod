"""
AskMaddi Pipeline — Cross-Platform Dedup
==========================================
Matches products across Amazon and eBay using a layered strategy:
  Level 1: Model number exact match (highest confidence)
  Level 2: Brand + normalized title similarity
  Level 3: UPC/EAN match (when available)
  Level 4: Brand + price proximity + category (lowest confidence)
"""

import re
from typing import Optional
from .models import ScrapedProduct, UnifiedProduct


# --- Title Normalization ---

_FILLER_WORDS = {
    'with', 'for', 'and', 'the', 'new', 'latest', 'best', 'top',
    'premium', 'professional', 'pro', 'edition', 'version', 'series',
    'original', 'genuine', 'authentic', 'official', 'updated',
}

_NOISE_PATTERN = re.compile(r'[^\w\s-]')  # Remove punctuation except hyphens


def normalize_title(title: str) -> str:
    """Strip filler words, punctuation, and normalize whitespace."""
    t = title.lower()
    t = _NOISE_PATTERN.sub(' ', t)
    tokens = [w for w in t.split() if w not in _FILLER_WORDS and len(w) > 1]
    return ' '.join(tokens)


def extract_brand(title: str) -> str:
    """Extract brand from title (usually first 1-2 words)."""
    # Common pattern: brand is the first capitalized word(s) before model/description
    words = title.strip().split()
    if not words:
        return ""
    # First word is usually the brand
    return words[0].strip().lower()


def extract_model(title: str) -> str:
    """Extract model number from title.

    Looks for patterns like: WH-1000XM5, KBD75v2, RTX-3070, G502, etc.
    """
    # Alphanumeric sequences with hyphens that look like model numbers
    patterns = [
        r'\b([A-Z]{1,4}[-]?\d{2,5}[A-Z]?\d*[A-Z]*)\b',  # WH-1000XM5, G502
        r'\b(\d{2,4}[A-Z]+\d*)\b',                         # 510BT
        r'\b([A-Z]+\d+[A-Z]+\d*)\b',                       # XM5
    ]
    for pattern in patterns:
        m = re.search(pattern, title, re.IGNORECASE)
        if m:
            return m.group(1).upper()
    return ""


# --- Similarity ---

def token_overlap(a: str, b: str) -> float:
    """Token overlap ratio between two normalized strings."""
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


# --- Matching ---

def match_products(amazon_products: list, ebay_products: list,
                   model_threshold: float = 1.0,
                   title_threshold: float = 0.5,
                   price_proximity: float = 0.15) -> list:
    """Match products across platforms and produce UnifiedProduct records.

    Returns a list of UnifiedProduct, including unmatched products from
    both platforms as single-source records.
    """
    unified = []
    matched_ebay_ids = set()

    for amz in amazon_products:
        amz_title_norm = normalize_title(amz.title)
        amz_brand = extract_brand(amz.title) or amz.brand.lower()
        amz_model = extract_model(amz.title) or amz.model.upper()

        best_match = None
        best_confidence = 0.0
        best_ebay = None

        for ebay in ebay_products:
            if id(ebay) in matched_ebay_ids:
                continue

            ebay_title_norm = normalize_title(ebay.title)
            ebay_brand = extract_brand(ebay.title) or ebay.brand.lower()
            ebay_model = extract_model(ebay.title) or ebay.model.upper()

            confidence = 0.0

            # Level 1: Model number exact match
            if amz_model and ebay_model and amz_model == ebay_model:
                confidence = 0.95
            # Level 2: Brand + title similarity
            elif amz_brand and amz_brand == ebay_brand:
                sim = token_overlap(amz_title_norm, ebay_title_norm)
                if sim >= title_threshold:
                    confidence = 0.5 + (sim * 0.4)  # 0.5 - 0.9 range
            # Level 4: Brand + price proximity
            elif (amz_brand and amz_brand == ebay_brand
                  and amz.price > 0 and ebay.price > 0):
                price_ratio = min(amz.price, ebay.price) / max(amz.price, ebay.price)
                if price_ratio >= (1 - price_proximity):
                    confidence = 0.3

            if confidence > best_confidence:
                best_confidence = confidence
                best_ebay = ebay

        # Build unified record
        if best_ebay and best_confidence >= 0.3:
            matched_ebay_ids.add(id(best_ebay))
            unified_product = UnifiedProduct(
                canonical_title=_canonical_title(amz, best_ebay),
                brand=amz.brand or extract_brand(amz.title),
                model=amz_model or extract_model(best_ebay.title),
                category=amz.category,
                amazon=amz,
                ebay=best_ebay,
                match_confidence=best_confidence,
            )
        else:
            unified_product = UnifiedProduct(
                canonical_title=_clean_title(amz.title),
                brand=amz.brand or extract_brand(amz.title),
                model=amz_model,
                category=amz.category,
                amazon=amz,
            )

        unified_product.compute_cross_platform()
        unified.append(unified_product)

    # Add unmatched eBay products
    for ebay in ebay_products:
        if id(ebay) not in matched_ebay_ids:
            p = UnifiedProduct(
                canonical_title=_clean_title(ebay.title),
                brand=ebay.brand or extract_brand(ebay.title),
                model=extract_model(ebay.title),
                category=ebay.category,
                ebay=ebay,
            )
            p.compute_cross_platform()
            unified.append(p)

    return unified


def _canonical_title(amz: ScrapedProduct, ebay: ScrapedProduct) -> str:
    """Build canonical title from the shorter, cleaner source."""
    # Amazon titles tend to be keyword-stuffed; eBay often cleaner
    a = _clean_title(amz.title)
    b = _clean_title(ebay.title)
    return a if len(a) <= len(b) else b


def _clean_title(title: str) -> str:
    """Light cleanup of a product title — remove excess commas, normalize spaces."""
    t = re.sub(r'\s+', ' ', title.strip())
    # Truncate at first comma after 40 chars if title is very long
    if len(t) > 80 and ',' in t[40:]:
        idx = t.index(',', 40)
        t = t[:idx]
    return t
