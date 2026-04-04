"""
AskMaddi Pipeline — Weight Normalization
==========================================
Parses raw weight strings from Amazon/eBay into (ounces, grams).
Handles: "4.5 pounds", "4 lbs 9 oz", "2.07 kg", "73 ounces",
         "2,070 grams", "1.3 Kilograms", "11.2 oz", etc.
"""

import re
from typing import Optional, Tuple

# Conversion constants
OZ_PER_LB = 16.0
OZ_PER_KG = 35.274
OZ_PER_GRAM = 0.035274
GRAMS_PER_OZ = 28.3495

# Patterns for compound weights like "4 lbs 9 oz", "4 pounds 9 ounces"
_COMPOUND_LB_OZ = re.compile(
    r'(\d+\.?\d*)\s*(?:lbs?|pounds?)\s+(\d+\.?\d*)\s*(?:oz|ounces?)',
    re.IGNORECASE
)

# Pattern for single unit weights
_SINGLE_UNIT = re.compile(
    r'(\d[\d,]*\.?\d*)\s*(lbs?|pounds?|oz|ounces?|kg|kilograms?|grams?|g)\b',
    re.IGNORECASE
)


def normalize_weight(raw: str) -> Tuple[Optional[float], Optional[int]]:
    """Parse weight string into (ounces, grams).

    Returns (None, None) if unparseable.

    Examples:
        >>> normalize_weight("4 lbs 9 oz")
        (73.0, 2070)
        >>> normalize_weight("2.07 kg")
        (73.02, 2070)
        >>> normalize_weight("73 ounces")
        (73.0, 2069)
        >>> normalize_weight("1,200 grams")
        (42.33, 1200)
        >>> normalize_weight("4.5 pounds")
        (72.0, 2041)
    """
    if not raw or not raw.strip():
        return None, None

    raw = raw.strip()

    # Try compound format first: "4 lbs 9 oz"
    m = _COMPOUND_LB_OZ.search(raw)
    if m:
        lbs = float(m.group(1))
        oz = float(m.group(2))
        total_oz = lbs * OZ_PER_LB + oz
        total_grams = int(round(total_oz * GRAMS_PER_OZ))
        return round(total_oz, 1), total_grams

    # Try single unit
    m = _SINGLE_UNIT.search(raw)
    if m:
        value_str = m.group(1).replace(',', '')
        value = float(value_str)
        unit = m.group(2).lower()

        if unit in ('lb', 'lbs', 'pound', 'pounds'):
            oz = value * OZ_PER_LB
            grams = int(round(oz * GRAMS_PER_OZ))
            return round(oz, 1), grams

        elif unit in ('oz', 'ounce', 'ounces'):
            grams = int(round(value * GRAMS_PER_OZ))
            return round(value, 1), grams

        elif unit in ('kg', 'kilogram', 'kilograms'):
            oz = value * OZ_PER_KG
            grams = int(round(value * 1000))
            return round(oz, 2), grams

        elif unit in ('g', 'gram', 'grams'):
            oz = value * OZ_PER_GRAM
            grams = int(round(value))
            return round(oz, 2), grams

    return None, None


def format_weight(oz: Optional[float], grams: Optional[int],
                  relevance: str = "medium") -> str:
    """Format weight for display in product cards.

    Returns empty string if weight not available or relevance too low.
    Shows both oz and grams for cross-community readability.
    """
    if oz is None or grams is None:
        return ""

    # Only show weight when category relevance warrants it
    if relevance == "low":
        return ""

    # Format based on magnitude
    if oz >= OZ_PER_LB:
        lbs = int(oz // OZ_PER_LB)
        remaining_oz = oz % OZ_PER_LB
        if remaining_oz > 0.5:
            weight_str = f"{lbs} lbs {remaining_oz:.0f} oz ({grams:,} g)"
        else:
            weight_str = f"{lbs} lbs ({grams:,} g)"
    else:
        weight_str = f"{oz:.1f} oz ({grams:,} g)"

    return f"⚖️ {weight_str}"
