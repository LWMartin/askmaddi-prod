"""
AskMaddi Pipeline — Output Formatter
======================================
Generates blog posts, RSS entries, and social teasers from ranked products.

Blog posts go to askmaddi.com/compare/{category-slug}/{date}
Social teasers route to platform-specific formats.
"""

import json
from datetime import date
from typing import Optional
from .models import UnifiedProduct
from .weight import format_weight
from .scoring import FLAG_DISPLAY


# --- Affiliate Link Wrapping ---

AFFILIATE_CONFIG = {
    'amazon': {'param': 'tag', 'code': 'askmaddi-20'},
    'ebay': {'param': 'campid', 'code': '5339138080',
             'extra': {'toolid': '10001', 'mkevt': '1'}},
}


def affiliate_url(url: str, source: str) -> str:
    """Append affiliate tracking to a product URL."""
    config = AFFILIATE_CONFIG.get(source)
    if not config:
        return url

    separator = '&' if '?' in url else '?'
    result = f"{url}{separator}{config['param']}={config['code']}"

    if 'extra' in config:
        for k, v in config['extra'].items():
            result += f"&{k}={v}"

    return result


# --- Blog Post ---

def format_blog_post(products: list, category_name: str,
                     category_slug: str, post_date: Optional[date] = None,
                     weight_relevance: str = "medium") -> str:
    """Generate full blog post HTML for a Top 10 comparison."""
    if post_date is None:
        post_date = date.today()

    date_str = post_date.strftime("%B %d, %Y")
    url_date = post_date.isoformat()

    lines = []
    lines.append(f'<article class="comparison-post" data-category="{category_slug}" data-date="{url_date}">')
    lines.append(f'<h1>Top {len(products)} {category_name} — Amazon vs eBay Compared ({date_str})</h1>')
    lines.append('')

    # Product cards
    for i, p in enumerate(products, 1):
        lines.append(f'<div class="product-card" data-rank="{i}">')
        lines.append(f'  <h3>{i}. {p.canonical_title}</h3>')

        # Rating line
        src = p.best_source
        if src:
            stars = '⭐' * int(src.rating)
            lines.append(f'  <p class="rating">{stars} {src.rating:.1f} ({src.review_count:,} reviews) | Quality Score: {p.quality_score:.0f}/100</p>')

        # Weight (when relevant)
        weight_str = format_weight(p.weight_oz, p.weight_grams, weight_relevance)
        if weight_str:
            lines.append(f'  <p class="weight">{weight_str}</p>')

        # Platform pricing
        lines.append('  <div class="pricing">')
        if p.amazon:
            aff_url = affiliate_url(p.amazon.url, 'amazon')
            lines.append(f'    <p>Amazon: <a href="{aff_url}" rel="nofollow sponsored">${p.amazon.price:.2f}</a></p>')
        if p.ebay:
            aff_url = affiliate_url(p.ebay.url, 'ebay')
            seller_note = ""
            if p.ebay.seller_rating and p.ebay.seller_rating >= 99:
                seller_note = " (Top Rated Seller)"
            lines.append(f'    <p>eBay: <a href="{aff_url}" rel="nofollow sponsored">${p.ebay.price:.2f}</a>{seller_note}</p>')
        lines.append('  </div>')

        # Insight line
        if p.both_platforms and p.price_delta > 5:
            lines.append(f'  <p class="insight">💰 ${p.price_delta:.0f} cheaper on {p.cheaper_on.title()}</p>')

        # Warning flags
        warnings = [f for f in p.quality_flags if f in ('sketchy-seller', 'title-spam', 'new-listing')]
        for w in warnings:
            lines.append(f'  <p class="warning">{FLAG_DISPLAY.get(w, w)}</p>')

        lines.append('</div>')
        lines.append('')

    # Summary section
    lines.append('<div class="summary">')
    lines.append('<h2>Summary</h2>')

    if products:
        best_overall = products[0]
        lines.append(f'<p><strong>Best overall:</strong> {best_overall.canonical_title}</p>')

        # Best value = highest score among lowest-priced third
        by_price = sorted(products, key=lambda p: p.best_price)
        value_pick = max(by_price[:max(3, len(by_price) // 3)],
                        key=lambda p: p.quality_score)
        lines.append(f'<p><strong>Best value:</strong> {value_pick.canonical_title}</p>')

        # Biggest price gap
        cross_platform = [p for p in products if p.both_platforms and p.price_delta > 0]
        if cross_platform:
            biggest_gap = max(cross_platform, key=lambda p: p.price_delta)
            lines.append(f'<p><strong>Biggest price gap:</strong> {biggest_gap.canonical_title} — '
                        f'${biggest_gap.price_delta:.0f} cheaper on {biggest_gap.cheaper_on.title()}</p>')

    lines.append('</div>')

    # Methodology
    lines.append('<div class="methodology">')
    lines.append(f'<p><em>Prices and ratings scraped {date_str}. Rankings based on review quality, '
                 'cross-platform availability, seller reputation, and price competitiveness. '
                 'AskMaddi is reader-supported — links may earn a commission.</em></p>')
    lines.append('</div>')

    # JSON-LD structured data
    lines.append('')
    lines.append('<script type="application/ld+json">')
    lines.append(json.dumps(_build_jsonld(products, category_name, post_date), indent=2))
    lines.append('</script>')

    lines.append('</article>')
    return '\n'.join(lines)


def _build_jsonld(products: list, category_name: str, post_date: date) -> dict:
    """Build JSON-LD ItemList for Google rich snippets."""
    items = []
    for i, p in enumerate(products, 1):
        offers = []
        if p.amazon:
            offers.append({
                "@type": "Offer",
                "price": f"{p.amazon.price:.2f}",
                "priceCurrency": "USD",
                "seller": {"@type": "Organization", "name": "Amazon"},
                "url": affiliate_url(p.amazon.url, 'amazon'),
            })
        if p.ebay:
            offers.append({
                "@type": "Offer",
                "price": f"{p.ebay.price:.2f}",
                "priceCurrency": "USD",
                "seller": {"@type": "Organization", "name": "eBay"},
                "url": affiliate_url(p.ebay.url, 'ebay'),
            })

        item = {
            "@type": "ListItem",
            "position": i,
            "item": {
                "@type": "Product",
                "name": p.canonical_title,
                "offers": offers,
            }
        }
        if p.brand:
            item["item"]["brand"] = {"@type": "Brand", "name": p.brand}
        src = p.best_source
        if src and src.review_count > 0:
            item["item"]["aggregateRating"] = {
                "@type": "AggregateRating",
                "ratingValue": f"{src.rating:.1f}",
                "reviewCount": str(src.review_count),
            }
        items.append(item)

    return {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": f"Top {len(products)} {category_name} — Amazon vs eBay",
        "datePublished": post_date.isoformat(),
        "itemListElement": items,
    }


# --- RSS Entry ---

def format_rss_entry(products: list, category_name: str,
                     category_slug: str, post_date: Optional[date] = None) -> str:
    """Generate a single RSS <item> entry."""
    if post_date is None:
        post_date = date.today()

    url = f"https://askmaddi.com/compare/{category_slug}/{post_date.isoformat()}"

    # Find highlights for custom namespace fields
    top_pick = products[0].canonical_title if products else ""
    best_value = ""
    biggest_saving = ""
    saving_platform = ""
    saving_amount = 0

    if products:
        by_price = sorted(products, key=lambda p: p.best_price)
        value_candidates = by_price[:max(3, len(products) // 3)]
        if value_candidates:
            best_value = max(value_candidates, key=lambda p: p.quality_score).canonical_title

        cross = [p for p in products if p.both_platforms and p.price_delta > 0]
        if cross:
            gap = max(cross, key=lambda p: p.price_delta)
            biggest_saving = gap.canonical_title
            saving_platform = gap.cheaper_on
            saving_amount = gap.price_delta

    pub_date = post_date.strftime("%a, %d %b %Y 07:00:00 -0600")

    lines = [
        '  <item>',
        f'    <title>Top {len(products)} {category_name} — {post_date.strftime("%B %d, %Y")}</title>',
        f'    <link>{url}</link>',
        f'    <guid isPermaLink="true">{url}</guid>',
        f'    <pubDate>{pub_date}</pubDate>',
        f'    <category>{category_name}</category>',
        f'    <maddi:topPick>{top_pick}</maddi:topPick>',
        f'    <maddi:bestValue>{best_value}</maddi:bestValue>',
    ]
    if biggest_saving:
        lines.append(f'    <maddi:biggestSaving platform="{saving_platform}" amount="{saving_amount:.2f}">{biggest_saving}</maddi:biggestSaving>')
    lines.extend([
        f'    <maddi:categorySlug>{category_slug}</maddi:categorySlug>',
        f'    <maddi:productCount>{len(products)}</maddi:productCount>',
        '  </item>',
    ])
    return '\n'.join(lines)


# --- Social Teasers ---

def format_reddit_post(products: list, category_name: str,
                       category_slug: str, post_date: Optional[date] = None) -> dict:
    """Generate Reddit post title and body."""
    if post_date is None:
        post_date = date.today()

    url = f"https://askmaddi.com/compare/{category_slug}/{post_date.isoformat()}"
    day_name = post_date.strftime("%A")

    title = (f"We compared the top {len(products)} {category_name.lower()} "
             f"across Amazon and eBay — the price gaps are wild")

    body_lines = [
        f"Every {day_name} we scrape both platforms and score products on review "
        "quality, seller legitimacy, and cross-platform pricing. Here's what stood "
        "out this week:",
        "",
    ]

    if products:
        top = products[0]
        body_lines.append(f"🏆 Best overall: {top.canonical_title} — "
                         f"{top.best_rating:.1f} stars, {top.best_review_count:,} reviews")

        cross = [p for p in products if p.both_platforms and p.price_delta > 0]
        if cross:
            gap = max(cross, key=lambda p: p.price_delta)
            body_lines.append(f"💰 Biggest savings: {gap.canonical_title} — "
                            f"${gap.price_delta:.0f} cheaper on {gap.cheaper_on.title()}")

        warnings = [p for p in products if 'sketchy-seller' in p.quality_flags
                    or 'new-listing' in p.quality_flags]
        if warnings:
            w = warnings[0]
            flag_text = "great reviews but low seller rating" if 'sketchy-seller' in w.quality_flags else "very few reviews"
            body_lines.append(f"⚠️ Surprising find: {w.canonical_title} — {flag_text}")

    body_lines.extend([
        "",
        f"Full breakdown with all {len(products)} products and comparison:",
        url,
        "",
        "---",
        "We're building a product comparison engine that actually filters the garbage. "
        "Feedback welcome.",
    ])

    return {
        'title': title,
        'body': '\n'.join(body_lines),
        'url': url,
    }


def format_x_post(products: list, category_name: str,
                  category_slug: str, post_date: Optional[date] = None) -> str:
    """Generate X (Twitter) post text."""
    if post_date is None:
        post_date = date.today()

    url = f"https://askmaddi.com/compare/{category_slug}/{post_date.isoformat()}"

    text = f"We compared the top {len(products)} {category_name.lower()} across Amazon & eBay today.\n\n"

    cross = [p for p in products if p.both_platforms and p.price_delta > 0]
    if cross:
        gap = max(cross, key=lambda p: p.price_delta)
        seller_note = ""
        if gap.ebay and gap.ebay.seller_rating and gap.ebay.seller_rating >= 99:
            seller_note = ", with a Top Rated Seller"
        text += (f"Biggest find: {gap.canonical_title} is ${gap.price_delta:.0f} "
                f"cheaper on {gap.cheaper_on.title()} right now{seller_note}.\n\n")
    elif products:
        text += f"Best overall: {products[0].canonical_title}\n\n"

    text += f"Full breakdown → {url}"
    return text
