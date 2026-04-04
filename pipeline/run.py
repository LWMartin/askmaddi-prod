#!/usr/bin/env python3
"""
AskMaddi Distribution Engine — Daily Pipeline Runner
======================================================
Designed to run via cron at 4:00 AM daily.

    0 4 * * 1-5 /opt/askmaddi/pipeline/run.py >> /var/log/askmaddi/pipeline.log 2>&1

Pipeline stages:
    1. Calendar  → determine today's category
    2. Scrape    → headless Chrome fetches Amazon + eBay search results
    3. Extract   → parse product data from HTML
    4. Dedup     → cross-platform matching
    5. Score     → quality scoring + flags
    6. Rank      → top 10 with brand diversity
    7. Format    → blog post HTML + RSS entry + social teasers
    8. Publish   → write to askmaddi.com (Phase 2)
    9. Social    → post teasers to Reddit/X (Phase 3)

Current status: Stages 1, 4-7 are functional. Stage 2-3 requires
headless Chrome (pending AlmaLinux 8 migration + Chrome install).
Stages 8-9 require API credentials and publisher module.
"""

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

from pipeline.calendar import get_todays_category
from pipeline.dedup import match_products
from pipeline.scoring import rank_products
from pipeline.formatter import (
    format_blog_post,
    format_rss_entry,
    format_reddit_post,
    format_x_post,
)


# --- Configuration ---

OUTPUT_DIR = os.environ.get('ASKMADDI_OUTPUT', '/opt/askmaddi/output')
DATA_DIR = os.environ.get('ASKMADDI_DATA', '/opt/askmaddi/data')


def ensure_dirs():
    """Create output directories if they don't exist."""
    for d in [OUTPUT_DIR, DATA_DIR,
              os.path.join(OUTPUT_DIR, 'posts'),
              os.path.join(OUTPUT_DIR, 'social'),
              os.path.join(DATA_DIR, 'raw'),
              os.path.join(DATA_DIR, 'products')]:
        os.makedirs(d, exist_ok=True)


# --- Stage 2-3: Scraping (STUB) ---

def scrape_category(search_terms: list, category_slug: str) -> tuple:
    """Scrape Amazon and eBay for the given search terms.

    Returns (amazon_products, ebay_products) as lists of ScrapedProduct.

    TODO: Implement headless Chrome scraping after AlmaLinux 8 migration.
    Currently returns empty lists — use load_test_data() for development.
    """
    print(f"  [STUB] Scraper not yet implemented — needs headless Chrome")
    print(f"  [STUB] Would search: {search_terms}")
    return [], []


def load_test_data(category_slug: str) -> tuple:
    """Load previously saved scrape data for testing pipeline stages 4-7.

    Save test data to: {DATA_DIR}/raw/{category_slug}_amazon.json
                       {DATA_DIR}/raw/{category_slug}_ebay.json
    """
    from pipeline.models import ScrapedProduct

    amazon_products = []
    ebay_products = []

    amazon_file = os.path.join(DATA_DIR, 'raw', f'{category_slug}_amazon.json')
    ebay_file = os.path.join(DATA_DIR, 'raw', f'{category_slug}_ebay.json')

    for filepath, products in [(amazon_file, amazon_products), (ebay_file, ebay_products)]:
        if os.path.exists(filepath):
            with open(filepath, 'r') as f:
                data = json.load(f)
                for item in data:
                    products.append(ScrapedProduct(**item))
            print(f"  Loaded {len(products)} products from {filepath}")
        else:
            print(f"  No test data at {filepath}")

    return amazon_products, ebay_products


# --- Main Pipeline ---

def run_pipeline(target_date: date = None, dry_run: bool = False,
                 use_test_data: bool = False) -> dict:
    """Execute the full daily pipeline.

    Args:
        target_date: Override date (default: today)
        dry_run: Generate output but don't publish
        use_test_data: Load saved data instead of scraping

    Returns:
        dict with pipeline results and output paths
    """
    if target_date is None:
        target_date = date.today()

    print(f"=== AskMaddi Pipeline — {target_date.isoformat()} ===")
    print(f"  Time: {datetime.now().strftime('%H:%M:%S')}")

    # Stage 1: Calendar
    entry = get_todays_category(target_date)
    if entry is None:
        print("  No category scheduled for today (weekend). Exiting.")
        return {'status': 'skip', 'reason': 'weekend'}

    category = entry['category']
    cat_name = entry['effective_name']
    cat_slug = entry['effective_slug']
    search_terms = entry['search_terms']

    print(f"  Category: {cat_name}")
    print(f"  Slug: {cat_slug}")
    print(f"  Search terms: {search_terms}")

    # Stage 2-3: Scrape (or load test data)
    if use_test_data:
        amazon_products, ebay_products = load_test_data(cat_slug)
    else:
        amazon_products, ebay_products = scrape_category(search_terms, cat_slug)

    total_scraped = len(amazon_products) + len(ebay_products)
    print(f"  Scraped: {len(amazon_products)} Amazon, {len(ebay_products)} eBay ({total_scraped} total)")

    if total_scraped == 0:
        print("  No products scraped. Exiting.")
        return {'status': 'error', 'reason': 'no_products'}

    # Stage 4: Dedup
    unified = match_products(amazon_products, ebay_products)
    cross_platform = sum(1 for p in unified if p.both_platforms)
    print(f"  Unified: {len(unified)} products ({cross_platform} cross-platform matches)")

    # Stage 5-6: Score + Rank
    top_products = rank_products(unified, top_n=10, max_per_brand=3)
    print(f"  Ranked: Top {len(top_products)} selected")

    # Stage 7: Format
    weight_rel = category.weight_relevance

    blog_html = format_blog_post(top_products, cat_name, cat_slug,
                                  target_date, weight_rel)
    rss_entry = format_rss_entry(top_products, cat_name, cat_slug, target_date)
    reddit = format_reddit_post(top_products, cat_name, cat_slug, target_date)
    x_post = format_x_post(top_products, cat_name, cat_slug, target_date)

    # Write outputs
    ensure_dirs()
    results = {'status': 'ok', 'category': cat_name, 'date': target_date.isoformat()}

    post_path = os.path.join(OUTPUT_DIR, 'posts', f'{cat_slug}_{target_date.isoformat()}.html')
    with open(post_path, 'w') as f:
        f.write(blog_html)
    results['blog_post'] = post_path
    print(f"  Blog post: {post_path}")

    rss_path = os.path.join(OUTPUT_DIR, 'posts', f'{cat_slug}_{target_date.isoformat()}_rss.xml')
    with open(rss_path, 'w') as f:
        f.write(rss_entry)
    results['rss_entry'] = rss_path

    social_path = os.path.join(OUTPUT_DIR, 'social', f'{cat_slug}_{target_date.isoformat()}.json')
    with open(social_path, 'w') as f:
        json.dump({'reddit': reddit, 'x': x_post}, f, indent=2)
    results['social'] = social_path
    print(f"  Social teasers: {social_path}")

    # Save product data for historical tracking
    products_path = os.path.join(DATA_DIR, 'products', f'{cat_slug}_{target_date.isoformat()}.json')
    product_data = []
    for p in top_products:
        entry = {
            'rank': top_products.index(p) + 1,
            'title': p.canonical_title,
            'brand': p.brand,
            'model': p.model,
            'quality_score': p.quality_score,
            'quality_flags': p.quality_flags,
            'amazon_price': p.amazon.price if p.amazon else None,
            'ebay_price': p.ebay.price if p.ebay else None,
            'price_delta': p.price_delta,
            'cheaper_on': p.cheaper_on,
            'both_platforms': p.both_platforms,
            'weight_oz': p.weight_oz,
            'weight_grams': p.weight_grams,
            'rating': p.best_rating,
            'review_count': p.best_review_count,
        }
        product_data.append(entry)

    with open(products_path, 'w') as f:
        json.dump(product_data, f, indent=2)
    results['product_data'] = products_path
    print(f"  Product data: {products_path}")

    if dry_run:
        print("  [DRY RUN] Skipping publish + social post")
    else:
        # Stage 8: Publish (TODO)
        print("  [TODO] Publish to askmaddi.com")
        # Stage 9: Social (TODO)
        print("  [TODO] Post to Reddit/X")

    print(f"=== Pipeline complete ===")
    return results


# --- CLI ---

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='AskMaddi Daily Pipeline')
    parser.add_argument('--date', type=str, default=None,
                        help='Override date (YYYY-MM-DD)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Generate output but skip publish/social')
    parser.add_argument('--test-data', action='store_true',
                        help='Load saved test data instead of scraping')
    parser.add_argument('--schedule', type=int, default=0,
                        help='Preview category schedule for N days')

    args = parser.parse_args()

    if args.schedule > 0:
        from pipeline.calendar import get_schedule
        schedule = get_schedule(days=args.schedule)
        for entry in schedule:
            print(f"  {entry['date']} ({entry['day']:9s})  {entry['category']}")
        sys.exit(0)

    target = None
    if args.date:
        target = date.fromisoformat(args.date)

    result = run_pipeline(target_date=target, dry_run=args.dry_run,
                          use_test_data=args.test_data)
    sys.exit(0 if result['status'] in ('ok', 'skip') else 1)
