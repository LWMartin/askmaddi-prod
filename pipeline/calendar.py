"""
AskMaddi Pipeline — Category Calendar & Configuration
=======================================================
10 categories on a 2-week A/B rotation. Hiking gear has a 10-week
sub-category cycle within its Friday slot.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional


# --- Category Definitions ---

@dataclass
class Category:
    name: str
    slug: str
    search_terms: list
    price_range: tuple          # (min, max) in USD
    weight_relevance: str       # "critical", "high", "medium", "low"
    subreddits: list
    week: str                   # "A" or "B"
    day: int                    # 0=Mon, 1=Tue, ..., 4=Fri


CATEGORIES = [
    # Week A: Mon-Fri
    Category(
        name="Wireless Headphones",
        slug="wireless-headphones",
        search_terms=["wireless headphones", "bluetooth headphones"],
        price_range=(30, 400),
        weight_relevance="low-medium",
        subreddits=["r/headphones", "r/budgetaudiophile", "r/HeadphoneAdvice"],
        week="A", day=0,
    ),
    Category(
        name="Laptops Under $1000",
        slug="laptops-under-1000",
        search_terms=["laptop under 1000", "budget laptop"],
        price_range=(300, 999),
        weight_relevance="high",
        subreddits=["r/laptops", "r/SuggestALaptop", "r/GamingLaptops"],
        week="A", day=1,
    ),
    Category(
        name="Mechanical Keyboards",
        slug="mechanical-keyboards",
        search_terms=["mechanical keyboard", "gaming keyboard mechanical"],
        price_range=(50, 300),
        weight_relevance="medium",
        subreddits=["r/MechanicalKeyboards", "r/BudgetKeebs"],
        week="A", day=2,
    ),
    Category(
        name="Bluetooth Speakers",
        slug="bluetooth-speakers",
        search_terms=["bluetooth speaker", "portable speaker"],
        price_range=(30, 300),
        weight_relevance="high",
        subreddits=["r/bluetooth_speakers", "r/BudgetAudiophile"],
        week="A", day=3,
    ),
    Category(
        name="Gaming Monitors",
        slug="gaming-monitors",
        search_terms=["gaming monitor", "144hz monitor"],
        price_range=(150, 500),
        weight_relevance="low",
        subreddits=["r/Monitors", "r/buildapcsales"],
        week="A", day=4,
    ),

    # Week B: Mon-Fri
    Category(
        name="Small Kitchen Appliances",
        slug="small-kitchen-appliances",
        search_terms=["air fryer", "instant pot", "blender"],
        price_range=(40, 300),
        weight_relevance="medium",
        subreddits=["r/BuyItForLife", "r/Cooking", "r/airfryer"],
        week="B", day=0,
    ),
    Category(
        name="Power Tools",
        slug="power-tools",
        search_terms=["cordless drill", "power tool set"],
        price_range=(80, 500),
        weight_relevance="high",
        subreddits=["r/Tools", "r/woodworking"],
        week="B", day=1,
    ),
    Category(
        name="Espresso Machines",
        slug="espresso-machines",
        search_terms=["espresso machine", "espresso maker"],
        price_range=(100, 500),
        weight_relevance="low-medium",
        subreddits=["r/espresso", "r/Coffee"],
        week="B", day=2,
    ),
    Category(
        name="Sewing Machines",
        slug="sewing-machines",
        search_terms=["sewing machine", "quilting machine"],
        price_range=(100, 500),
        weight_relevance="medium",
        subreddits=["r/sewing", "r/quilting"],
        week="B", day=3,
    ),
    Category(
        name="Hiking Gear",
        slug="hiking-gear",
        search_terms=[],  # Uses sub-category rotation
        price_range=(50, 500),
        weight_relevance="critical",
        subreddits=["r/Ultralight", "r/CampingGear", "r/hiking", "r/WildernessBackpacking"],
        week="B", day=4,
    ),
]


# --- Hiking Gear Sub-Category Rotation ---

@dataclass
class HikingSubCategory:
    name: str
    slug: str
    search_terms: list
    weight_relevance: str


HIKING_SUBCATEGORIES = [
    HikingSubCategory("Ultralight Tents", "ultralight-tents",
                       ["ultralight tent", "backpacking tent"], "critical"),
    HikingSubCategory("Sleeping Bags & Quilts", "sleeping-bags-quilts",
                       ["down sleeping bag", "ultralight quilt"], "critical"),
    HikingSubCategory("Sleeping Pads", "sleeping-pads",
                       ["sleeping pad", "ultralight sleeping pad"], "critical"),
    HikingSubCategory("Down Jackets", "down-jackets",
                       ["down jacket", "ultralight down jacket"], "critical"),
    HikingSubCategory("Backpacks", "backpacks",
                       ["backpacking pack 65L", "ultralight backpack"], "critical"),
    HikingSubCategory("Trekking Poles", "trekking-poles",
                       ["trekking poles", "carbon trekking poles"], "high"),
    HikingSubCategory("Water Filters", "water-filters",
                       ["backpacking water filter", "water purifier hiking"], "high"),
    HikingSubCategory("Camp Stoves", "camp-stoves",
                       ["backpacking stove", "ultralight stove"], "critical"),
    HikingSubCategory("Rain Gear", "rain-gear",
                       ["rain jacket hiking", "Gore-Tex jacket"], "high"),
    HikingSubCategory("Headlamps", "headlamps",
                       ["headlamp hiking", "rechargeable headlamp"], "medium"),
]


# --- Calendar Logic ---

# Reference epoch: a known Monday that starts Week A.
# Any Monday works — we just need a consistent anchor.
_EPOCH = date(2026, 4, 6)  # A Monday, Week A


def get_week_type(d: date) -> str:
    """Determine if a date falls in Week A or Week B."""
    # Weeks alternate A/B starting from epoch
    days_since = (d - _EPOCH).days
    week_num = days_since // 7
    return "A" if week_num % 2 == 0 else "B"


def get_hiking_subcategory_index(d: date) -> int:
    """Get which hiking sub-category index for a given date.

    Cycles through 10 sub-categories, one per Week B Friday.
    Revisits every 20 weeks.
    """
    # Count how many Week B Fridays have passed since epoch
    days_since = (d - _EPOCH).days
    week_num = days_since // 7
    # Only Week B weeks count
    b_week_count = week_num // 2  # Every other week is B
    return b_week_count % len(HIKING_SUBCATEGORIES)


def get_todays_category(d: Optional[date] = None) -> Optional[dict]:
    """Get the category for a given date (or today).

    Returns None for weekends.
    Returns dict with 'category', and optionally 'subcategory' for hiking gear.
    """
    if d is None:
        d = date.today()

    weekday = d.weekday()  # 0=Mon ... 6=Sun
    if weekday > 4:
        return None  # No posts on weekends

    week_type = get_week_type(d)

    # Find matching category
    for cat in CATEGORIES:
        if cat.week == week_type and cat.day == weekday:
            result = {
                'category': cat,
                'search_terms': cat.search_terms,
                'date': d,
                'week_type': week_type,
            }

            # Hiking gear uses sub-category rotation
            if cat.slug == "hiking-gear":
                idx = get_hiking_subcategory_index(d)
                sub = HIKING_SUBCATEGORIES[idx]
                result['subcategory'] = sub
                result['search_terms'] = sub.search_terms
                result['effective_name'] = f"Hiking Gear: {sub.name}"
                result['effective_slug'] = f"hiking-gear-{sub.slug}"
            else:
                result['effective_name'] = cat.name
                result['effective_slug'] = cat.slug

            return result

    return None


def get_schedule(start: Optional[date] = None, days: int = 14) -> list:
    """Preview the next N days of the category calendar."""
    if start is None:
        start = date.today()

    schedule = []
    for i in range(days):
        d = start + timedelta(days=i)
        entry = get_todays_category(d)
        if entry:
            schedule.append({
                'date': d.isoformat(),
                'day': d.strftime('%A'),
                'category': entry['effective_name'],
                'slug': entry['effective_slug'],
                'search_terms': entry['search_terms'],
            })
    return schedule


# --- CLI Preview ---

if __name__ == "__main__":
    import json
    print("=== AskMaddi Category Calendar ===")
    print(f"Starting from: {date.today().isoformat()}")
    print()
    schedule = get_schedule(days=28)
    for entry in schedule:
        print(f"  {entry['date']} ({entry['day']:9s})  {entry['category']}")
        print(f"    Search: {entry['search_terms']}")
    print(f"\n{len(schedule)} posts over 28 days")
