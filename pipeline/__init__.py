"""
AskMaddi Distribution Engine Pipeline
=======================================
Automated daily scrape → dedup → score → rank → format → publish.

Usage:
    from pipeline import get_todays_category, rank_products, format_blog_post

See pipeline/run.py for the full daily pipeline runner.
"""

from .models import ScrapedProduct, UnifiedProduct
from .calendar import get_todays_category, get_schedule, CATEGORIES, HIKING_SUBCATEGORIES
from .dedup import match_products
from .scoring import score_and_flag, rank_products
from .weight import normalize_weight, format_weight
from .formatter import (
    format_blog_post,
    format_rss_entry,
    format_reddit_post,
    format_x_post,
    affiliate_url,
)

__all__ = [
    'ScrapedProduct',
    'UnifiedProduct',
    'get_todays_category',
    'get_schedule',
    'CATEGORIES',
    'HIKING_SUBCATEGORIES',
    'match_products',
    'score_and_flag',
    'rank_products',
    'normalize_weight',
    'format_weight',
    'format_blog_post',
    'format_rss_entry',
    'format_reddit_post',
    'format_x_post',
    'affiliate_url',
]
