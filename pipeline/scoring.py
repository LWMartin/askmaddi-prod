"""
AskMaddi Pipeline — Quality Scoring
=====================================
Pre-Knowledge-Core scoring computed from scraped signals.
When the .ramish Product Knowledge Core ships, swap this module
for geometric trust scoring — UnifiedProduct interface stays the same.
"""

from .models import UnifiedProduct, ScrapedProduct
from typing import Optional


def compute_quality_score(product: UnifiedProduct) -> float:
    """Compute composite quality score (0-100) from scraped signals.

    Scoring philosophy: penalize suspicious patterns, reward legitimate
    signals. Cross-platform presence is a strong legitimacy signal.
    """
    score = 50.0  # Base

    src = product.best_source
    if src is None:
        return score

    # --- Review quality (max +20 / -15) ---
    if src.review_count > 100 and src.rating >= 4.0:
        score += 20
    elif src.review_count > 50 and src.rating >= 3.5:
        score += 10
    elif src.review_count < 10:
        score -= 15  # Too few reviews to trust

    # Suspiciously perfect reviews
    if src.rating >= 4.8 and src.review_count > 500:
        score -= 10  # Likely gamed

    # --- Cross-platform presence (+10) ---
    if product.both_platforms:
        score += 10  # Exists on both = more legitimate

    # --- Seller quality (max +10 / -15) ---
    if product.amazon:
        seller_lower = product.amazon.seller_name.lower()
        if "sold by amazon" in seller_lower or "ships from amazon" in seller_lower:
            score += 10

    if product.ebay:
        if product.ebay.seller_rating and product.ebay.seller_rating < 95:
            score -= 15  # Low eBay seller rating = risky

    # --- Title quality (max -10) ---
    title = src.title
    if len(title) > 120 or title.count(",") > 3:
        score -= 10  # Keyword-stuffed title

    # --- Sponsored penalty (-5) ---
    if src.is_sponsored:
        score -= 5

    # --- Price sanity (-5) ---
    # Products priced at $0.01 or suspiciously low are likely listing errors
    if src.price > 0 and src.price < 5:
        score -= 5

    return max(0.0, min(100.0, score))


def compute_quality_flags(product: UnifiedProduct) -> list:
    """Generate human-readable quality flags for display."""
    flags = []
    src = product.best_source
    if src is None:
        return flags

    # Positive flags
    if product.price_delta > 10 and product.both_platforms:
        flags.append("price-winner")

    if product.best_rating >= 4.3 and product.best_review_count > 200:
        flags.append("great-reviews")

    if product.both_platforms:
        flags.append("cross-platform")

    if product.amazon and product.amazon.is_amazons_choice:
        flags.append("amazon-choice")

    if product.amazon and product.amazon.is_best_seller:
        flags.append("best-seller")

    # Neutral flags
    if product.best_review_count < 15:
        flags.append("new-listing")

    # Warning flags
    if product.ebay and product.ebay.seller_rating and product.ebay.seller_rating < 95:
        flags.append("sketchy-seller")

    if src.title and len(src.title) > 120:
        flags.append("title-spam")

    if src.is_sponsored:
        flags.append("sponsored")

    return flags


FLAG_DISPLAY = {
    "price-winner":   "💰 Significant price difference between platforms",
    "great-reviews":  "⭐ Highly rated with strong review volume",
    "cross-platform": "✅ Available on both Amazon and eBay",
    "amazon-choice":  "🏆 Amazon's Choice",
    "best-seller":    "🏆 Best Seller on Amazon",
    "new-listing":    "🆕 Few reviews — newer or less popular listing",
    "sketchy-seller": "⚠️ Low seller rating on eBay",
    "title-spam":     "⚠️ Keyword-stuffed title — common on knockoffs",
    "sponsored":      "📢 Sponsored listing",
}


def score_and_flag(product: UnifiedProduct) -> UnifiedProduct:
    """Compute score and flags, update product in place, return it."""
    product.quality_score = compute_quality_score(product)
    product.quality_flags = compute_quality_flags(product)
    return product


def rank_products(products: list, top_n: int = 10,
                  max_per_brand: int = 3) -> list:
    """Select top N products by quality score with brand diversity.

    No more than max_per_brand products from the same brand in the final list.
    """
    # Score all products
    for p in products:
        score_and_flag(p)

    # Sort by quality score descending
    sorted_products = sorted(products, key=lambda p: p.quality_score, reverse=True)

    # Apply brand diversity filter
    result = []
    brand_counts = {}

    for p in sorted_products:
        brand = (p.brand or "unknown").lower().strip()
        count = brand_counts.get(brand, 0)
        if count >= max_per_brand:
            continue
        result.append(p)
        brand_counts[brand] = count + 1
        if len(result) >= top_n:
            break

    return result
