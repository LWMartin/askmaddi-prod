"""
AskMaddi Pipeline — Data Models
================================
Core dataclasses for the scraping → dedup → scoring → formatting pipeline.
Designed from maddi-distribution-engine spec (2026-03-29).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class ScrapedProduct:
    """Raw product data from a single platform."""

    # Identity
    source: str                     # "amazon" | "ebay"
    source_id: str                  # ASIN or eBay item ID
    url: str                        # Full product URL (base for affiliate link)
    title: str                      # Raw product title
    brand: str = ""                 # Extracted brand name
    model: str = ""                 # Model number if parseable

    # Pricing
    price: float = 0.0              # Current price in USD
    list_price: float = 0.0         # Original/list price (for discount calc)
    shipping: str = ""              # "free", "calculated", or dollar amount

    # Quality signals
    rating: float = 0.0             # Average star rating (0-5)
    review_count: int = 0           # Number of reviews/ratings
    seller_name: str = ""           # Who's selling it
    seller_rating: float = 0.0      # Seller feedback score
    condition: str = "new"          # "new", "renewed", "used", etc.
    is_sponsored: bool = False      # Sponsored/promoted listing
    is_amazons_choice: bool = False # Amazon's Choice badge
    is_best_seller: bool = False    # Best Seller badge

    # Metadata
    image_url: str = ""             # Primary product image
    category: str = ""              # Category as shown on platform
    scraped_at: Optional[datetime] = None

    # Physical specs
    weight_oz: Optional[float] = None     # Normalized weight in ounces
    weight_grams: Optional[int] = None    # Normalized weight in grams
    weight_raw: str = ""                  # Raw weight string as scraped


@dataclass
class UnifiedProduct:
    """Merged product record after cross-platform dedup."""

    # Canonical identity
    canonical_title: str            # Cleaned, normalized product name
    brand: str = ""
    model: str = ""
    category: str = ""

    # Per-platform data
    amazon: Optional[ScrapedProduct] = None
    ebay: Optional[ScrapedProduct] = None

    # Cross-platform insights
    price_delta: float = 0.0        # Absolute price difference
    cheaper_on: str = ""            # "amazon" | "ebay" | "same"
    match_confidence: float = 0.0   # 0-1, from matching level
    both_platforms: bool = False     # True if found on both

    # Quality composite
    quality_score: float = 50.0     # 0-100 composite score
    quality_flags: list = field(default_factory=list)

    # Weight (from best source)
    weight_oz: Optional[float] = None
    weight_grams: Optional[int] = None
    weight_raw: str = ""

    @property
    def best_source(self) -> Optional[ScrapedProduct]:
        """Return the source with the most data (prefer Amazon for review depth)."""
        if self.amazon and self.ebay:
            if self.amazon.review_count >= self.ebay.review_count:
                return self.amazon
            return self.ebay
        return self.amazon or self.ebay

    @property
    def best_price(self) -> float:
        """Lowest price across platforms."""
        prices = []
        if self.amazon and self.amazon.price > 0:
            prices.append(self.amazon.price)
        if self.ebay and self.ebay.price > 0:
            prices.append(self.ebay.price)
        return min(prices) if prices else 0.0

    @property
    def best_rating(self) -> float:
        """Highest rating across platforms."""
        ratings = []
        if self.amazon and self.amazon.rating > 0:
            ratings.append(self.amazon.rating)
        if self.ebay and self.ebay.rating > 0:
            ratings.append(self.ebay.rating)
        return max(ratings) if ratings else 0.0

    @property
    def best_review_count(self) -> int:
        """Highest review count across platforms."""
        counts = []
        if self.amazon:
            counts.append(self.amazon.review_count)
        if self.ebay:
            counts.append(self.ebay.review_count)
        return max(counts) if counts else 0

    def compute_cross_platform(self):
        """Calculate price delta and cheaper_on after both platforms are set."""
        if self.amazon and self.ebay and self.amazon.price > 0 and self.ebay.price > 0:
            self.both_platforms = True
            self.price_delta = abs(self.amazon.price - self.ebay.price)
            if self.amazon.price < self.ebay.price:
                self.cheaper_on = "amazon"
            elif self.ebay.price < self.amazon.price:
                self.cheaper_on = "ebay"
            else:
                self.cheaper_on = "same"
        elif self.amazon:
            self.cheaper_on = "amazon"
        elif self.ebay:
            self.cheaper_on = "ebay"

        # Pull weight from best available source
        src = self.amazon or self.ebay
        if src:
            self.weight_oz = src.weight_oz
            self.weight_grams = src.weight_grams
            self.weight_raw = src.weight_raw
