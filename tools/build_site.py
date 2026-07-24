#!/usr/bin/env python3
"""
build_site.py — AskMaddi static card-detail page generator (Phase 2 + Phase 4).

Reads full card JSON (the pipeline's card.vN.json shape) and emits a static,
crawlable detail page at  browser/cards/{card_id}/index.html  plus refreshes
browser/cards-manifest.json (the teaser-grid summary the homepage consumes).

Design: pure static HTML + the existing browser/css/maddi.css design system.
No client-side JSON loading — fast, shareable, SEO-friendly. Card detail uses
a small dedicated stylesheet (cards-detail.css) that extends, not replaces, the
shared tokens in maddi.css.

Usage:
    python tools/build_site.py --cards-dir data/cards/ --output-dir browser/
    python tools/build_site.py --card path/to/card.v4.json --output-dir browser/

Real-data discipline (the v1 photography corpus is honest about its gaps):
  - Missing pricing (0.0 / None)  -> render a "Check current price" CTA, never "$0".
  - confidence == "low"           -> show an honest confidence badge, don't hide it.
  - issue_clusters empty          -> omit the section rather than show an empty box.
  - We render exactly what the card says; we never invent data to fill the template.
"""

import argparse
import html
import json
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path


# ─── Affiliate tags (match the live frontend / cards-manifest) ──────────────
AMAZON_TAG = "askmaddi-20"
EBAY_CAMPID = "5339138080"
EBAY_RID = "711-53200-19255-0"

# ─── Site identity (canonical URLs, OG, sitemap) ────────────────────────────
BASE_URL = "https://askmaddi.com"
SITE_NAME = "AskMaddi"


def abs_url(path_or_url):
    """Absolute URL for OG/JSON-LD/sitemap. Pass-through if already absolute."""
    if not path_or_url:
        return ""
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
    return BASE_URL + ("" if path_or_url.startswith("/") else "/") + path_or_url


def fmt_date_human(iso_str):
    """ISO timestamp -> 'Jun 10, 2026'. Empty string on anything unparseable."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        return f"{dt:%b} {dt.day}, {dt.year}"
    except (ValueError, TypeError):
        return ""


def used_price_asof(card):
    """ISO date the used band was fetched, or '' (honest absence, no fallback
    to build time — a build is not a price observation)."""
    pricing = card.get("pricing", {}) or {}
    used = pricing.get("used_market", {}) or {}
    return used.get("price_updated_at") or ""


def ensure_affiliate_tag(url):
    """Guarantee the affiliate tag on any amazon/ebay URL, idempotently.

    Cards built on-demand carry raw product URLs in pricing.current_new_url
    (cron populates affiliate_url later, or never for fresh builds). Every
    CTA URL must pass through here so no untagged link reaches the page.
    Sets params via urllib so existing tags are overwritten, never doubled.
    """
    if not url:
        return url
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = parts.netloc.lower()
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    if "amazon." in host:
        q["tag"] = AMAZON_TAG
    elif "ebay." in host:
        q["campid"] = EBAY_CAMPID
        q.setdefault("mkcid", "1")
        q.setdefault("mkrid", EBAY_RID)
    else:
        return url
    return urlunsplit(parts._replace(query=urlencode(q)))

# Source-type display grouping for the Sources section.
SOURCE_TYPE_LABELS = {
    "lab": ("\U0001F52C", "Lab & Measurement"),       # microscope
    "pro": ("\U0001F4F9", "Pro Reviews"),             # video camera
    "youtube": ("\U0001F4F9", "YouTube"),
    "blog": ("\U0001F4F0", "Blogs & Publications"),   # newspaper
    "editorial": ("\U0001F4F0", "Editorial"),
    "reddit": ("\U0001F4AC", "Reddit & Forums"),      # speech balloon
    "forum": ("\U0001F4AC", "Forums"),
    "manufacturer": ("\U0001F3ED", "Manufacturer"),   # factory
}
SOURCE_TYPE_FALLBACK = ("\U0001F517", "Other Sources")  # link


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def _reviewer_from_source_id(source_id):
    """Derive a readable reviewer name from a source_id slug.

    e.g. 'youtube-tony-chelsea-northrup-sigma-35-mm-...' -> 'Tony Chelsea Northrup'.
    Strips the leading platform token and any trailing product/year tokens by
    keeping only the name-ish prefix (up to the first product keyword).
    Best-effort: the goal is a tidy attribution, not perfect parsing.
    """
    if not source_id:
        return "source"
    parts = source_id.split("-")
    # drop leading platform token (youtube/blog/reddit/lab/etc.)
    if parts and parts[0] in {"youtube", "blog", "reddit", "lab", "forum", "editorial", "pro", "manufacturer", "yt"}:
        parts = parts[1:]
    # keep tokens until we hit something that looks like product/spec noise
    name_tokens = []
    for tok in parts:
        if tok.isdigit() or re.match(r"^\d", tok) or tok in {"mm", "f", "sigma", "sony", "canon", "nikon", "vs", "review", "lens"}:
            break
        name_tokens.append(tok)
    if not name_tokens:
        name_tokens = parts[:3]
    return " ".join(t.capitalize() for t in name_tokens) or "source"


def pct(pos, total):
    return round(100 * pos / total) if total else 0


# ─── Pricing helpers (degrade gracefully on missing data) ───────────────────
def amazon_product_url(asin):
    """Direct product-detail (SKU) URL. Tag is stamped by ensure_affiliate_tag."""
    return f"https://www.amazon.com/dp/{asin}"


AMAZON_FIRST = True
# 2026-07-17 (Lee's call): during the Amazon Associates qualification push,
# the 'buy new' CTA deliberately prefers Amazon rungs over the eBay EPN
# affiliate chain — three qualifying purchases unlock Associates status and
# PA-API access. This forgoes eBay commission on new-CTA clicks by explicit
# strategy, and is a ONE-LINE revert (False) the day PA-API lands. The used
# CTA is untouched either way: the used market is eBay's domain and our
# used-price bands are eBay-derived.


def amazon_gtin_search_url(gtin, display_name=""):
    """Amazon search keyed on a VERIFIED GTIN plus the product name — a
    graceful-degrading query (2026-07-17, Lee's call): when Amazon knows
    the GTIN the exact product dominates the results; when it doesn't,
    the name tokens produce an honest close-match page instead of the
    dead 'no results' void a bare-GTIN query strands buyers on. The /dp/
    ASIN rung above remains the premium, human-verified landing; bare
    name search (no GTIN) stays last-resort per e930bea."""
    q = urllib.parse.quote_plus(f"{gtin} {display_name}".strip())
    return f"https://www.amazon.com/s?k={q}&tag={AMAZON_TAG}"


def amazon_search_url(display_name):
    q = re.sub(r"\s+", "+", display_name.strip())
    return f"https://www.amazon.com/s?k={q}&tag={AMAZON_TAG}"


def ebay_search_url(display_name, category_id=None):
    """Tagged eBay website-search CTA, scoped to `display_name`.

    For RANGE cards (e.g. Peak Design Pro = Lite/Pro/Tall) there is no single
    canonical product page, so a search that shows all variants is the honest
    target — but a bare keyword search drags in accessories (the live Browse
    probe returned a leveling base, a quick-release plate, and a backpack under
    "Peak Design Pro Tripod"). Pinning eBay's category via `_sacat` is the
    durable fix: it leans on eBay's own taxonomy instead of brittle hand-tuned
    `-plate -base` keyword exclusions that silently over-filter and rot. Camera
    Tripods & Monopods = 30093. Affiliate params stamped by ensure_affiliate_tag.
    """
    q = re.sub(r"\s+", "+", display_name.strip().lower())
    sacat = f"&_sacat={category_id}" if category_id else ""
    return (
        f"https://www.ebay.com/sch/i.html?_nkw={q}{sacat}"
        f"&mkcid=1&mkrid={EBAY_RID}&campid={EBAY_CAMPID}"
    )


# eBay leaf category ids for CTA scoping (eBay's own taxonomy, stable).
EBAY_CATEGORY = {
    "support": "30093",   # Camera Tripods & Monopods
}


def ebay_product_url(epid):
    """Direct eBay catalog (EPID) product page — the eBay analogue of
    amazon_product_url(asin). Lands the buyer on the catalog product page
    instead of search results. Affiliate params are stamped by
    ensure_affiliate_tag (campid/mkcid/mkrid), same as every other eBay CTA.
    EPID is eBay's durable catalog id; a single listing can vanish but the
    catalog entry persists, so this is the right rung to store in the registry.
    """
    return f"https://www.ebay.com/p/{epid}"


def new_cta(card):
    """Return (label, url) for the 'buy new' CTA, honest about missing price.

    URL preference:
      explicit affiliate_url > raw product URL > Amazon ASIN /dp/ link
      > eBay EPID /p/ link > eBay search > Amazon search (last resort).

    The ASIN rung (pricing.amazon_asin) lands the buyer on the exact Amazon SKU
    page; same field the future PA-API price job keys on. When a card has NO
    Amazon ASIN — because the product isn't sold on Amazon (e.g. Peak Design Pro
    Tripod) — falling to Amazon search would dump the buyer onto a results page
    whose top hit is a DIFFERENT product (the Travel Tripod). So the no-ASIN
    path resolves to eBay instead: a catalog EPID /p/ deep-link when the
    registry carries one, else an eBay search scoped to this product. Amazon
    search remains only as the final last-resort rung for the degenerate case
    where neither marketplace id is available. All URLs pass ensure_affiliate_tag.
    """
    pricing = card.get("pricing", {})
    name = card["identity"]["display_name"]
    asin = pricing.get("amazon_asin")
    gtin = pricing.get("gtin")
    epid = pricing.get("ebay_epid")
    ebay_cat = EBAY_CATEGORY.get(card.get("category"))
    amazon_ok = not pricing.get("amazon_absent")
    if AMAZON_FIRST and amazon_ok:
        url = ensure_affiliate_tag(
            (amazon_product_url(asin) if asin else None)
            or (amazon_gtin_search_url(gtin, name) if gtin else None)
            or pricing.get("affiliate_url")
            or pricing.get("current_new_url")
            or (ebay_product_url(epid) if epid else None)
            or ebay_search_url(name, ebay_cat)
        )
    elif AMAZON_FIRST:
        # Registry-verified absent from Amazon: every Amazon rung skipped —
        # a close-match page for an absent product is the wrong-product trap.
        url = ensure_affiliate_tag(
            pricing.get("affiliate_url")
            or pricing.get("current_new_url")
            or (ebay_product_url(epid) if epid else None)
            or ebay_search_url(name, ebay_cat)
        )
    else:
        # Historical ladder (e930bea): explicit URLs outrank marketplace ids.
        url = ensure_affiliate_tag(
            pricing.get("affiliate_url")
            or pricing.get("current_new_url")
            or (amazon_product_url(asin) if asin else None)
            or (ebay_product_url(epid) if epid else None)
            or (ebay_search_url(name, ebay_cat) if not asin else None)
            or amazon_search_url(name)
        )
    price = pricing.get("current_new_usd") or pricing.get("msrp_usd") or 0
    label = f"${int(price)} new" if price and price > 0 else "Check current price"
    return label, url


def used_cta(card):
    """Return (label, url) for the 'used' CTA, honest about missing price."""
    pricing = card.get("pricing", {})
    used = pricing.get("used_market", {}) or {}
    name = card["identity"]["display_name"]
    ebay_cat = EBAY_CATEGORY.get(card.get("category"))
    url = ensure_affiliate_tag(
        used.get("affiliate_url") or used.get("search_url") or ebay_search_url(name, ebay_cat)
    )
    # Prefer a representative band midpoint if present; else just label "used".
    bands = used.get("bands", {}) or {}
    band_prices = [v for v in bands.values() if isinstance(v, (int, float)) and v > 0]
    if band_prices:
        label = f"from ${int(min(band_prices))} used"
    else:
        label = "See used"
    return label, url


# ─── HTML fragment builders ─────────────────────────────────────────────────
def confidence_badge(level):
    level = (level or "unknown").lower()
    cls = {
        "high": "conf-high",
        "medium": "conf-med",
        "med": "conf-med",
        "low": "conf-low",
    }.get(level, "conf-unknown")
    return f'<span class="conf-badge {cls}">{esc(level)} confidence</span>'


def axis_block(axis):
    """One axis: label + confidence + sentiment bar + counts + top quote."""
    name = axis.get("display_name") or axis.get("axis_id", "")
    sent = axis.get("sentiment", {}) or {}
    pos, neu, neg = sent.get("pos", 0), sent.get("neu", 0), sent.get("neg", 0)
    total = sent.get("total", pos + neu + neg)
    p = pct(pos, total)
    n = pct(neg, total)

    # Card-face blurb: the pipeline stamps a gated `face_quote` per axis
    # (unambiguous-subset doctrine, 2026-06-10) with a display-ready
    # excerpt and a joined review URL. The renderer shows it or shows
    # NOTHING — it never scans raw evidence refs for display text, so an
    # axis without a gate-passing quote suppresses its blurb and the
    # sentiment bar carries the axis alone. When a URL is present the
    # whole blurb is the link (like what you hear -> click through).
    quote_html = ""
    fq = axis.get("face_quote") or {}
    q = str(fq.get("quote_excerpt") or "").strip()
    if q:
        reviewer = fq.get("reviewer") or _reviewer_from_source_id(fq.get("source_id", ""))
        url = fq.get("url", "")
        cite = f'<span class="quote-cite">\u2014 {esc(reviewer)}</span>'
        if url:
            quote_html = (
                f'<blockquote class="axis-quote axis-quote-linked">'
                f'<a class="quote-link" href="{esc(url)}" target="_blank" '
                f'rel="nofollow noopener">\u201c{esc(q)}\u201d {cite}</a>'
                f'</blockquote>'
            )
        else:
            quote_html = (
                f'<blockquote class="axis-quote">\u201c{esc(q)}\u201d {cite}</blockquote>'
            )

    return f"""
        <div class="detail-axis">
          <div class="detail-axis-head">
            <span class="detail-axis-name">{esc(name)}</span>
          </div>
          <div class="axis-row">
            <div class="axis-bar">
              <div class="bar-pos" style="width:{p}%"></div>
              <div class="bar-neg" style="width:{n}%"></div>
            </div>
            <span class="axis-counts">{pos} pos · {neu} neu · {neg} neg</span>
          </div>
          {quote_html}
        </div>"""


def sources_section(sources):
    """Group sources by type, each a clickable link to the original."""
    groups = {}
    for s in sources:
        st = (s.get("source_type") or s.get("platform") or "other").lower()
        groups.setdefault(st, []).append(s)

    blocks = []
    for st, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        icon, label = SOURCE_TYPE_LABELS.get(st, SOURCE_TYPE_FALLBACK)
        links = []
        for s in items:
            title = s.get("title") or s.get("reviewer") or s.get("source_id", "source")
            reviewer = s.get("reviewer", "")
            url = s.get("url", "")
            byline = f' <span class="src-by">{esc(reviewer)}</span>' if reviewer else ""
            if url:
                links.append(
                    f'<li><a href="{esc(url)}" target="_blank" rel="nofollow noopener">{esc(title)}</a>{byline}</li>'
                )
            else:
                links.append(f"<li>{esc(title)}{byline}</li>")
        blocks.append(
            f'<div class="src-group"><h3 class="src-group-head">{icon} {esc(label)} '
            f'<span class="src-count">({len(items)})</span></h3>'
            f'<ul class="src-list">{"".join(links)}</ul></div>'
        )
    return f'<div class="src-grid">{"".join(blocks)}</div>'


def price_history_section(card):
    used = (card.get("pricing", {}) or {}).get("used_market", {}) or {}
    bands = used.get("bands", {}) or {}
    if not bands:
        return ""
    cells = []
    for band, price in bands.items():
        val = f"${int(price)}" if isinstance(price, (int, float)) and price > 0 else "\u2014"
        cells.append(f'<div class="band"><span class="band-label">{esc(band)}</span><span class="band-price">{val}</span></div>')
    sold = used.get("sold_last_90d", 0)
    sold_line = f'<p class="band-note">{sold} sold in the last 90 days on {esc(used.get("source","eBay"))}.</p>' if sold else ""
    asof = fmt_date_human(used.get("price_updated_at") or "")
    asof_line = f'<p class="band-note">Prices as of {esc(asof)} — lowest active listing per condition.</p>' if asof else ""
    return f"""
      <section class="card-section">
        <h2 class="card-section-head">Used Market</h2>
        <div class="band-grid">{"".join(cells)}</div>
        {sold_line}
        {asof_line}
      </section>"""


# ─── Specs (product-forward; honest about absence) ──────────────────────────
# Specs are a facts-pipeline output (fact-pipeline §3). On an external card they
# live under `facts.specs` as a {slug: FactValue} map — FactValue is the dict
# {value, low, high, anchor, anchor_source, unit}. Numeric facts carry an honest
# spread (§3.3): the anchor (manufacturer value) is shown first, and a genuine
# [low, high] range is appended when third-party sources widen it. Categorical
# facts use `value`. When absent or empty the section is omitted entirely (same
# discipline as pricing/issue_clusters), never rendered as an empty box.

# Slugs that carry a canonical casing the naive title-case would mangle.
_SPEC_LABEL_OVERRIDES = {
    "iso": "ISO", "gtin": "GTIN", "mpn": "MPN", "af": "AF", "ois": "OIS",
    "ibis": "IBIS", "nd": "ND", "led": "LED", "usb": "USB", "hdmi": "HDMI",
    "fps": "FPS", "mp": "MP", "ev": "EV", "id": "ID",
}


def _spec_label(slug):
    """Humanize a spec slug (e.g. 'sensor_resolution' -> 'Sensor Resolution',
    'iso' -> 'ISO'). Known acronyms keep their canonical casing."""
    words = str(slug).replace("_", " ").replace("-", " ").split()
    return " ".join(_SPEC_LABEL_OVERRIDES.get(w.lower(), w.capitalize())
                    for w in words) or str(slug)


def _fmt_fact_num(n):
    """Render a numeric fact without false precision: drop a trailing '.0'
    (665.0 -> '665') but keep genuine decimals (66.5 -> '66.5')."""
    if isinstance(n, bool) or not isinstance(n, (int, float)):
        return esc(n)
    if isinstance(n, float) and n.is_integer():
        return str(int(n))
    return str(n)


def _fact_display(fv):
    """One FactValue -> display string, or '' if it carries nothing renderable.
    Accepts a plain scalar too (legacy flat {label: value} cards)."""
    if fv is None:
        return ""
    if not isinstance(fv, dict):
        # Legacy flat value — already a display-ready scalar.
        return "" if fv in ("", []) else esc(fv)
    unit = str(fv.get("unit") or "").strip()
    suffix = f" {esc(unit)}" if unit else ""
    anchor, low, high = fv.get("anchor"), fv.get("low"), fv.get("high")
    is_num = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool)
    if is_num(anchor) or is_num(low) or is_num(high):
        primary = anchor if is_num(anchor) else low
        out = f"{_fmt_fact_num(primary)}{suffix}"
        # Only surface the spread when sources genuinely disagree.
        if is_num(low) and is_num(high) and low != high:
            out += f" ({_fmt_fact_num(low)}–{_fmt_fact_num(high)})"
        return out
    value = fv.get("value")
    if value in (None, "", []):
        return ""
    return f"{esc(value)}{suffix}"


def _specs_provenance_line(facts):
    """Subtle 'specs: manufacturer · wikidata' subtitle (§3.2/§8) — the distinct
    sources backing the block, in first-seen order. '' when none recorded."""
    prov = facts.get("provenance") if isinstance(facts, dict) else None
    if not isinstance(prov, dict):
        return ""
    seen = []
    for key, entry in prov.items():
        # This is the "specs:" line — reflect ONLY spec sources. The provenance
        # map also carries identity.* entries (image/gtin/mpn fold, § wire);
        # those must not masquerade as a spec source here.
        if not str(key).startswith("specs."):
            continue
        src = (entry or {}).get("source") if isinstance(entry, dict) else None
        if src and src not in seen:
            seen.append(src)
    if not seen:
        return ""
    return (f'<p class="spec-provenance">specs: '
            f'{esc(" · ".join(seen))}</p>')


def specs_section(card):
    facts = card.get("facts") if isinstance(card.get("facts"), dict) else {}
    # Prefer the external envelope location; fall back to a top-level `specs`
    # for legacy/internal cards that never went through facts_from_card.
    specs = facts.get("specs") or card.get("specs") or {}
    rows = []
    if isinstance(specs, dict):
        items = specs.get("items", specs) if "items" in specs else specs
        if isinstance(items, dict):
            for slug, fv in items.items():
                disp = _fact_display(fv)
                if disp:
                    rows.append((_spec_label(slug), disp))
        elif isinstance(items, list):
            for d in items:
                disp = _fact_display(d.get("value"))
                if disp:
                    rows.append((d.get("label", ""), disp))
    elif isinstance(specs, list):
        for d in specs:
            disp = _fact_display(d.get("value"))
            if disp:
                rows.append((d.get("label", ""), disp))
    if not rows:
        return ""
    cells = "".join(
        f'<div class="spec-row"><span class="spec-label">{esc(label)}</span>'
        f'<span class="spec-value">{value}</span></div>'
        for label, value in rows
    )
    prov_html = _specs_provenance_line(facts)
    return f"""
      <section class="card-section">
        <h2 class="card-section-head">Specifications</h2>
        <div class="spec-grid">{cells}</div>{prov_html}
      </section>"""


def _has_sentiment(axis):
    """True if an axis carries any rated sentiment. Axes with total==0 are
    empty (no reviewer touched them) and are suppressed from the page entirely
    rather than rendered as a 0/0/0 bar."""
    return ((axis.get("sentiment", {}) or {}).get("total", 0) or 0) > 0


# Meta-axes suppressed from the card page entirely (ratified 2026-06-05).
# generation_context is extractor discourse-context, not a product quality —
# rendering it as a sentiment bar misleads. Distinct from TEASER_META_AXES:
# price stays visible on the page, it is only barred from teaser high/low roles.
CARD_HIDDEN_META_AXES = {"generation_context"}


def _card_visible(axis):
    return (_has_sentiment(axis)
            and (axis.get("axis_id") or "") not in CARD_HIDDEN_META_AXES)


# ─── Page assembly ──────────────────────────────────────────────────────────
# ─── schema.org Product/Offer JSON-LD ────────────────────────────────────────
# Honesty discipline carries through markup: we only assert prices we display.
# Used bands (eBay active asks, precision-gated upstream) -> AggregateOffer
# with UsedCondition. No bands -> Product without offers (Sigma-style gating).
# No ratings markup: our pos/neg ratios are not a star scale; inventing a
# mapping to win rich results would be result-picking. Offers only.
def schema_org_jsonld(card, canonical_url, img_url, description):
    ident = card["identity"]
    obj = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": ident["display_name"],
        "url": canonical_url,
    }
    if ident.get("brand"):
        obj["brand"] = {"@type": "Brand", "name": ident["brand"]}
    if img_url:
        obj["image"] = abs_url(img_url)
    if description:
        obj["description"] = description

    used = (card.get("pricing", {}) or {}).get("used_market", {}) or {}
    bands = used.get("bands", {}) or {}
    prices = [v for v in bands.values() if isinstance(v, (int, float)) and v > 0]
    if prices:
        _, used_url = used_cta(card)
        offer = {
            "@type": "AggregateOffer",
            "priceCurrency": "USD",
            "lowPrice": f"{min(prices):.2f}",
            "highPrice": f"{max(prices):.2f}",
            "itemCondition": "https://schema.org/UsedCondition",
            "availability": "https://schema.org/InStock",
            "url": used_url,
        }
        sample = used.get("sample_size")
        if isinstance(sample, int) and sample > 0:
            offer["offerCount"] = sample
        obj["offers"] = offer

    # `</` escaped so card text can never close the script element.
    return json.dumps(obj, indent=2).replace("</", "<\\/")


def render_page(card, image_url=None):
    ident = card["identity"]
    name = ident["display_name"]
    brand = ident.get("brand", "")
    subcat = (ident.get("subcategory") or "").title()
    cat = (ident.get("category") or "").title()
    year = ident.get("year_introduced") or ""
    descriptor = " · ".join([x for x in [brand, f"{subcat} {cat}".strip(), str(year)] if x])

    fresh = card.get("freshness", {}) or {}
    source_count = fresh.get("source_count", len(card.get("sources", [])))
    last_built = (fresh.get("last_built") or "")[:10]

    conf = (card.get("confidence", {}) or {}).get("overall", "unknown")

    new_label, new_url = new_cta(card)
    used_label, used_url = used_cta(card)

    synth = (card.get("synthesis", {}) or {}).get("consensus_paragraph", "")

    # Empty axes (sentiment.total == 0) are suppressed entirely — no reviewer
    # touched them, so rendering a 0/0/0 bar is noise, not information.
    # Meta-axes in CARD_HIDDEN_META_AXES are likewise suppressed from the page.
    lead = [a for a in (card.get("lead_axes", []) or []) if _card_visible(a)]
    detail = [a for a in (card.get("detail_axes", []) or []) if _card_visible(a)]

    lead_html = "\n".join(axis_block(a) for a in lead)
    detail_html = "\n".join(axis_block(a) for a in detail)

    # Issue clusters — omit entirely if empty (don't render an empty box).
    clusters = (card.get("synthesis", {}) or {}).get("issue_clusters", []) or []
    clusters_html = ""
    if clusters:
        items = "".join(
            f'<li class="issue"><span class="issue-warn">\u26a0</span> {esc(c.get("label", c.get("aspect","")))} '
            f'<span class="issue-cites">({c.get("citations", c.get("count",0))} citations)</span></li>'
            for c in clusters
        )
        clusters_html = f"""
      <section class="card-section">
        <h2 class="card-section-head">Common Concerns</h2>
        <ul class="issue-list">{items}</ul>
      </section>"""

    sources_html = sources_section(card.get("sources", []) or [])
    price_html = price_history_section(card)
    specs_html = specs_section(card)

    # Key Axes header only renders if at least one non-empty lead axis survives.
    lead_section = (
        f'''<section class="card-section">
        <h2 class="card-section-head">Key Axes</h2>
        <div class="axes-stack">{lead_html}</div>
      </section>''' if lead else ''
    )

    # Product image — use card identity image if present, else a placeholder block.
    # Product image precedence: card's own fields first (Phase 3 will populate
    # identity.image_hero), then an explicitly supplied URL (e.g. the
    # manufacturer shot already in cards-manifest.json), then placeholder.
    img_url = (ident.get("image_hero") or ident.get("image_thumb")
               or card.get("image_thumb") or image_url)
    img_html = (
        f'<img class="hero-product-img" src="{esc(img_url)}" alt="{esc(name)}" loading="eager">'
        if img_url else '<div class="hero-product-img placeholder">No image yet</div>'
    )

    meta_desc = (synth[:155] + "\u2026") if len(synth) > 155 else (synth or f"{name} — reviews synthesized from {source_count} sources.")

    canonical = f"{BASE_URL}/cards/{card['card_id']}/"
    asof_human = fmt_date_human(used_price_asof(card))
    _used_bands = ((card.get("pricing", {}) or {}).get("used_market", {}) or {}).get("bands", {}) or {}
    has_used_band = any(isinstance(v, (int, float)) and v > 0 for v in _used_bands.values())
    asof_html = (
        f'<p class="price-asof">Used price as of {esc(asof_human)} · active listings</p>'
        if (asof_human and has_used_band) else ""
    )
    jsonld = schema_org_jsonld(card, canonical, img_url, meta_desc)
    twitter_card = "summary_large_image" if img_url else "summary"

    # Phase 0 measurement seam (maddi-distribution v2.0): the BUILD writes the
    # category and per-CTA retailer as data-attributes; the beacon only ever
    # reads them. Category is the card's own lowercase category — never user
    # input — matching the /ping "category only, never the query" line.
    page_category = (ident.get("category") or "unknown").strip().lower()
    new_retailer = retailer_from_url(new_url)
    used_retailer = retailer_from_url(used_url)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{esc(name)} review — synthesized from {source_count} sources | AskMaddi</title>
  <meta name="description" content="{esc(meta_desc)}">
  <link rel="canonical" href="{esc(canonical)}">
  <meta property="og:title" content="{esc(name)} — AskMaddi">
  <meta property="og:description" content="{esc(meta_desc)}">
  <meta property="og:type" content="product">
  <meta property="og:url" content="{esc(canonical)}">
  <meta property="og:site_name" content="{SITE_NAME}">
  {f'<meta property="og:image" content="{esc(abs_url(img_url))}">' if img_url else ''}
  <meta name="twitter:card" content="{twitter_card}">
  <meta name="twitter:title" content="{esc(name)} — AskMaddi">
  <meta name="twitter:description" content="{esc(meta_desc)}">
  {f'<meta name="twitter:image" content="{esc(abs_url(img_url))}">' if img_url else ''}
  <script type="application/ld+json">
{jsonld}
  </script>
  <link rel="icon" type="image/png" href="/images/logo.png">
  <link rel="stylesheet" href="/css/maddi.css">
  <link rel="stylesheet" href="/css/cards-detail.css">
</head>
<body data-category="{esc(page_category)}">
  <div class="affiliate-disclosure-bar">Disclosure: We earn a commission when you buy through links on this page, at no cost to you.</div>
  <div class="container">

    <header class="header-compact">
      <a href="/" class="logo-title"><img src="/images/logo.png" alt="AskMaddi" class="site-logo">AskMaddi</a>
      <div class="search-box">
        <input type="text" id="detail-search-input" placeholder="Search another product\u2026" onkeydown="if(event.key==='Enter')document.getElementById('detail-search-button').click()">
        <button id="detail-search-button" onclick="location.href='/?q='+encodeURIComponent(document.getElementById('detail-search-input').value)">Ask Maddi</button>
      </div>
    </header>

    <article class="card-detail">

      <section class="card-hero">
        <div class="hero-media">{img_html}</div>
        <div class="hero-body">
          <h1 class="hero-title">{esc(name)}</h1>
          <p class="hero-descriptor">{esc(descriptor)}</p>
          <div class="hero-actions">
            <a class="btn-affiliate btn-buy-new" href="{esc(new_url)}" target="_blank" rel="nofollow noopener sponsored" data-out data-retailer="{new_retailer}" data-category="{esc(page_category)}">{esc(new_label)} \u2192</a>
            <a class="btn-affiliate btn-buy-used" href="{esc(used_url)}" target="_blank" rel="nofollow noopener sponsored" data-out data-retailer="{used_retailer}" data-category="{esc(page_category)}">{esc(used_label)} \u2192</a>
          </div>
          {asof_html}
          <p class="hero-meta">
            Synthesized from <strong>{source_count}</strong> reviewer sources
            {f"· Last updated {esc(last_built)}" if last_built else ""}
          </p>
        </div>
      </section>

      {specs_html}

      {f'''<section class="card-section">
        <h2 class="card-section-head">What reviewers agree on</h2>
        <p class="synthesis-text">{esc(synth)}</p>
      </section>''' if synth else ''}

      {lead_section}

      {clusters_html}

      {f'''<section class="card-section">
        <details class="detail-axes-toggle">
          <summary><h2 class="card-section-head inline">More detail — {len(detail)} additional axes</h2></summary>
          <div class="axes-stack">{detail_html}</div>
        </details>
      </section>''' if detail else ''}

      {price_html}

      <section class="card-section" id="sources">
        <h2 class="card-section-head">Sources <span class="src-total">({source_count})</span></h2>
        <p class="src-intro">Every claim above traces to these original reviews. We don't write opinions \u2014 we synthesize theirs.</p>
        {sources_html}
      </section>

    </article>

    <section class="card-section subscribe-section">
      <h2 class="card-section-head">New cards weekly</h2>
      <p class="subscribe-intro">Get each new review synthesis as it publishes. No spam, unsubscribe anytime.</p>
      <form data-subscribe class="subscribe-form" autocomplete="off">
        <input type="email" name="email" placeholder="you@example.com" required aria-label="Email address">
        <input type="text" name="website" tabindex="-1" autocomplete="off" aria-hidden="true" style="position:absolute;left:-9999px;opacity:0;height:0;width:0;">
        <button type="submit">Subscribe</button>
      </form>
      <p class="subscribe-note" aria-live="polite"></p>
    </section>

    <footer class="card-footer">
      <a href="/">\u2190 Back to AskMaddi</a>
      <span>·</span>
      <a href="/mission.html">Our method</a>
      <span>·</span>
      <a href="/why.html">Why AskMaddi</a>
    </footer>

  </div>
  <script src="/js/beacon.js" defer></script>
</body>
</html>
"""


# ─── Manifest (teaser grid) regeneration ────────────────────────────────────

# Axes excluded from the highest-rated / biggest-gripe teaser slots. These are
# meta-axes (relative context, value-for-money) — true but uninformative as a
# headline "high" or "low" next to concrete product qualities. They remain
# eligible for the most-discussed slot and always render on the detail page.
# Excluding CATEGORIES of axis by role is a design rule, not result-picking.
TEASER_META_AXES = {"generation_context", "price"}

TEASER_ROLE_MOST = "most_discussed"
TEASER_ROLE_HIGH = "highest_rated"
TEASER_ROLE_LOW = "biggest_gripe"


def select_teaser_axes(card):
    """Pick three role-based teaser axes (2026-06-03 design):

      1. most_discussed — highest claim volume (meta-axes eligible)
      2. highest_rated  — best pos-ratio among qualifying non-meta axes
      3. biggest_gripe  — worst pos-ratio among qualifying non-meta axes

    Qualifying = sentiment.total >= max(15, 0.1 * top axis volume). The
    relative floor scales across corpus sizes; the absolute floor stops a
    4-claim axis headlining a slot on noise.

    Collisions resolve to the next distinct axis (an axis fills one slot
    only). If fewer than three axes qualify, remaining slots fill in plain
    volume order with role=None (renderer shows no label on those).

    Returns a list of (axis_dict, role_or_None), length <= 3.
    """
    axes = (card.get("lead_axes") or []) + (card.get("detail_axes") or [])
    scored = []
    for a in axes:
        s = a.get("sentiment", {}) or {}
        total = s.get("total", 0) or 0
        if total <= 0:
            continue
        scored.append((a, total, (s.get("pos", 0) or 0) / total))
    if not scored:
        return []
    scored.sort(key=lambda t: t[1], reverse=True)
    floor = max(15, 0.1 * scored[0][1])
    qualifying = [t for t in scored if t[1] >= floor]

    def _aid(a):
        return a.get("axis_id") or a.get("display_name") or id(a)

    picks, used = [], set()

    def take(entry, role):
        used.add(_aid(entry[0]))
        picks.append((entry[0], role))

    if qualifying:
        take(qualifying[0], TEASER_ROLE_MOST)

    def pool():
        return [t for t in qualifying
                if _aid(t[0]) not in used
                and (t[0].get("axis_id") or "") not in TEASER_META_AXES]

    cands = pool()
    if cands:
        take(max(cands, key=lambda t: t[2]), TEASER_ROLE_HIGH)
    cands = pool()
    if cands:
        take(min(cands, key=lambda t: t[2]), TEASER_ROLE_LOW)

    # Sparse-card fallback: fill remaining slots in volume order, unlabeled.
    for t in scored:
        if len(picks) >= 3:
            break
        if _aid(t[0]) not in used:
            take(t, None)

    return picks


def teaser_entry(card):
    ident = card["identity"]
    # cards.js reads pricing.new_price / used_price (numbers) and runs its own
    # formatPrice() -> "$899" or "Check price" on zero/null. It appends the
    # " new" / " used" suffix itself, so we hand it raw numerics + URLs, NOT
    # the pre-labelled strings the detail page uses.
    pricing = card.get("pricing", {}) or {}
    new_price = pricing.get("current_new_usd") or pricing.get("msrp_usd") or 0
    _, new_url = new_cta(card)
    used = pricing.get("used_market", {}) or {}
    used_bands = [v for v in (used.get("bands", {}) or {}).values()
                  if isinstance(v, (int, float)) and v > 0]
    used_price = min(used_bands) if used_bands else 0
    _, used_url = used_cta(card)
    top = select_teaser_axes(card)
    return {
        "card_id": card["card_id"],
        "display_name": ident["display_name"],
        "brand": ident.get("brand", ""),
        "category": (ident.get("category") or "").title(),
        "subcategory": (ident.get("subcategory") or "").title(),
        "image_thumb": ident.get("image_thumb") or card.get("image_thumb") or "",
        "source_count": card.get("freshness", {}).get("source_count", len(card.get("sources", []))),
        "confidence": card.get("confidence", {}).get("overall", "unknown"),
        "top_axes": [
            {
                "axis": a.get("display_name") or a.get("axis_id"),
                "pos": (a.get("sentiment", {}) or {}).get("pos", 0),
                "neg": (a.get("sentiment", {}) or {}).get("neg", 0),
                "total": (a.get("sentiment", {}) or {}).get("total", 0),
                "role": role,
            }
            for a, role in top
        ],
        "pricing": {
            "new_price": int(new_price) if new_price else 0, "new_url": new_url,
            "used_price": int(used_price) if used_price else 0, "used_url": used_url,
        },
        "card_url": f"cards/{card['card_id']}/",
    }


def load_cards(args):
    cards = []
    if args.card:
        cards.append(json.loads(Path(args.card).read_text(encoding="utf-8")))
    if args.cards_dir:
        for p in sorted(Path(args.cards_dir).glob("*.json")):
            cards.append(json.loads(p.read_text(encoding="utf-8")))
    return cards


ASIN_REGISTRY_PATH = Path(__file__).parent.parent / "data" / "asin_registry.json"
EBAY_EPID_REGISTRY_PATH = Path(__file__).parent.parent / "data" / "ebay_epid_registry.json"


def apply_asin_registry(cards):
    """Inject durable Amazon ASINs into each card's pricing block.

    Aggregator rebuilds regenerate data/cards/*.json from extraction output,
    which carries no amazon_asin — so a backfilled ASIN is lost on every
    rebuild, and new_cta() falls through to a search-results URL (the tag
    survives but Amazon strips it on click-through to a product). The registry
    (data/asin_registry.json, keyed by card_id) is the durable source of truth
    re-applied at build time. A card that already carries an ASIN wins (a fresh
    PA-API value should never be clobbered by a static registry entry).
    """
    try:
        reg = json.loads(ASIN_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cards  # registry optional; absence degrades to prior behavior
    asins = reg.get("asins", {})
    absent = set(reg.get("absent", []))
    for card in cards:
        cid = card.get("card_id")
        if cid in absent:
            card.setdefault("pricing", {})["amazon_absent"] = True
        asin = asins.get(cid)
        if not asin:
            continue
        pricing = card.setdefault("pricing", {})
        if not pricing.get("amazon_asin"):
            pricing["amazon_asin"] = asin
    return cards


SKUS_SPINE_PATH = Path(__file__).parent.parent / "data" / "skus.json"


def apply_spine_gtins(cards):
    """Inject the spine's verified GTIN into each card's pricing block.

    Third sibling of the registry-merge pair below, but sourced from the
    LIVE spine (data/skus.json) rather than a hand-kept registry — GTINs
    are machine-verified with provenance receipts and survive rebuilds on
    the spine by construction. Enables the Amazon GTIN-search rung of
    new_cta under AMAZON_FIRST. A card already carrying a gtin wins.
    """
    try:
        skus = json.loads(SKUS_SPINE_PATH.read_text(encoding="utf-8")).get("skus", {})
    except (OSError, ValueError):
        return cards  # spine optional here; absence degrades the rung away
    for card in cards:
        entry = skus.get(card.get("card_id")) or {}
        g = entry.get("gtin")
        if g:
            pricing = card.setdefault("pricing", {})
            if not pricing.get("gtin"):
                pricing["gtin"] = g
    return cards


def apply_ebay_epid_registry(cards):
    """Inject durable eBay catalog EPIDs into each card's pricing block.

    The eBay analogue of apply_asin_registry. Same rebuild-survival rationale:
    aggregator rebuilds drop marketplace ids, so the durable EPID lives in
    data/ebay_epid_registry.json (keyed by card_id) and is re-applied here at
    build time. A card that already carries an ebay_epid wins (a live-resolved
    value should never be clobbered by a static registry entry). Absence is
    fine — new_cta() then degrades a no-ASIN card to a tagged eBay search rather
    than an eBay /p/ deep-link, which is still correct and earning-capable.
    """
    try:
        reg = json.loads(EBAY_EPID_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cards  # registry optional; absence degrades to prior behavior
    epids = reg.get("epids", {})
    for card in cards:
        cid = card.get("card_id")
        epid = epids.get(cid)
        if not epid:
            continue
        pricing = card.setdefault("pricing", {})
        if not pricing.get("ebay_epid"):
            pricing["ebay_epid"] = epid
    return cards


def card_lastmod(card):
    """Most recent observation date for a card: max(content build, price fetch).
    YYYY-MM-DD or '' — sitemap omits lastmod rather than inventing one."""
    fresh = (card.get("freshness", {}) or {}).get("last_built") or ""
    price = used_price_asof(card)
    best = max(d for d in (str(fresh), str(price), "")) if (fresh or price) else ""
    return best[:10]


SITEMAP_STATIC_PAGES = ["/", "/mission.html", "/privacy.html", "/terms.html"]


def write_sitemap(out_dir, cards):
    """browser/sitemap.xml — static pages + every card page, lastmod from card
    data. Derived artifact: regenerates from cards, so Stage 6 additions are
    indexed for free."""
    card_mods = {c["card_id"]: card_lastmod(c) for c in cards}
    home_mod = max([m for m in card_mods.values() if m], default="")

    def url_el(loc, lastmod=""):
        lm = f"\n    <lastmod>{lastmod}</lastmod>" if lastmod else ""
        return f"  <url>\n    <loc>{loc}</loc>{lm}\n  </url>"

    entries = [url_el(BASE_URL + "/", home_mod)]
    entries += [url_el(BASE_URL + p) for p in SITEMAP_STATIC_PAGES[1:]]
    entries += [url_el(f"{BASE_URL}/cards/{cid}/", mod) for cid, mod in sorted(card_mods.items())]

    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           + "\n".join(entries) + "\n</urlset>\n")
    path = Path(out_dir) / "sitemap.xml"
    path.write_text(xml, encoding="utf-8")
    return path


def retailer_from_url(url):
    """'amazon' | 'ebay' | 'other' from a CTA href's hostname.

    Feeds the data-retailer attribute the beacon reads; the gateway whitelists
    the same three values server-side (analytics_log.RETAILERS), so a surprise
    hostname degrades to 'other' at both ends rather than ever landing as
    free text. Hostname match, not substring-in-url: a search URL whose QUERY
    mentions amazon must not count as an Amazon click."""
    try:
        host = urllib.parse.urlparse(url or "").hostname or ""
    except ValueError:
        return "other"
    host = host.lower()
    if host == "amazon.com" or host.endswith(".amazon.com"):
        return "amazon"
    if host == "ebay.com" or host.endswith(".ebay.com"):
        return "ebay"
    return "other"


def write_llms_txt(out_dir, cards):
    """browser/llms.txt — the two-minute lottery ticket (maddi-distribution
    v2.0 §10 correction 1: crawlers overwhelmingly skip this file; shipped
    because it costs one function and might matter to future agents, carried
    with ZERO priority weight and no maintenance promise beyond this
    regeneration). Derived artifact like the sitemap: regenerates from cards."""
    lines = [
        "# AskMaddi",
        "",
        "> AskMaddi synthesizes product reviews from dozens of independent",
        "> sources into per-product cards: claim-level sentiment on concrete",
        "> axes, every claim attributed and linked to its original review,",
        "> used-price bands refreshed nightly. We aggregate others' assessments",
        "> and never write our own opinions; a human gate reviews every card",
        "> before publish. Affiliate-supported, disclosed on every page.",
        "",
        "## Cards",
        "",
    ]
    for c in sorted(cards, key=lambda c: c.get("card_id", "")):
        cid = c.get("card_id", "")
        name = ((c.get("identity", {}) or {}).get("display_name") or cid)
        n_src = c.get("freshness", {}).get("source_count",
                                           len(c.get("sources", []) or []))
        lines.append(f"- [{name}]({BASE_URL}/cards/{cid}/): review synthesis"
                     f" from {n_src} sources")
    lines += [
        "",
        "## Method",
        "",
        f"- [Why AskMaddi]({BASE_URL}/why.html): editorial philosophy",
        f"- [Our method]({BASE_URL}/mission.html): how synthesis works",
        "",
    ]
    path = Path(out_dir) / "llms.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main():
    ap = argparse.ArgumentParser(description="Generate AskMaddi card detail pages.")
    ap.add_argument("--card", help="Path to a single card JSON.")
    ap.add_argument("--cards-dir", help="Directory of card JSONs (*.json).")
    ap.add_argument("--output-dir", default="browser", help="Output root (default: browser/).")
    ap.add_argument("--manifest", action="store_true", help="Also regenerate cards-manifest.json.")
    ap.add_argument("--sitemap", action="store_true", help="Also regenerate sitemap.xml.")
    ap.add_argument("--image-url", help="Fallback product image URL when the card carries none "
                                        "(single-card builds only; card fields take precedence).")
    args = ap.parse_args()

    if not args.card and not args.cards_dir:
        ap.error("provide --card or --cards-dir")

    # ─── Clobber guard (2026-07-09 decision; in code 2026-07-15) ─────────────
    # cards-manifest.json and sitemap.xml are WHOLE-FILE outputs regenerated
    # from only the cards loaded THIS run. Composed with --card that means
    # "replace the site's entire grid/sitemap with this one card" — which is
    # never the intent and shrank the live homepage to a single card on the
    # gate's first real publish (7/03). Publishing a single card correctly is
    # admit-to-corpus THEN full --cards-dir rebuild (admin_surface.
    # build_site_runner does exactly this). Fail loud with the recipe.
    if args.card and (args.manifest or args.sitemap):
        ap.error(
            "--manifest/--sitemap regenerate WHOLE files from only the cards "
            "loaded this run; composed with --card they would replace the "
            "entire grid/sitemap with one entry. Use --cards-dir for "
            "manifest/sitemap builds. To publish one card: admit it into the "
            "corpus dir first, then rebuild with "
            "--cards-dir <corpus> --manifest --sitemap."
        )

    out = Path(args.output_dir)
    cards = load_cards(args)
    if not cards:
        print("No cards found.", file=sys.stderr)
        return 1
    apply_asin_registry(cards)
    apply_ebay_epid_registry(cards)
    apply_spine_gtins(cards)

    written = []
    for card in cards:
        cid = card["card_id"]
        page_dir = out / "cards" / cid
        page_dir.mkdir(parents=True, exist_ok=True)
        page = page_dir / "index.html"
        page.write_text(render_page(card, image_url=args.image_url), encoding="utf-8")
        written.append(str(page))
        print(f"  \u2713 {cid} \u2192 {page}")

    if args.manifest:
        manifest = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cards": [teaser_entry(c) for c in cards],
        }
        mpath = out / "cards-manifest.json"
        mpath.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"  \u2713 manifest \u2192 {mpath} ({len(cards)} cards)")

    if args.sitemap:
        spath = write_sitemap(out, cards)
        print(f"  \u2713 sitemap \u2192 {spath} ({len(cards)} card urls + {len(SITEMAP_STATIC_PAGES)} static)")
        # llms.txt rides the sitemap flag deliberately: identical whole-file
        # semantics (regenerated from the cards loaded THIS run), so it gets
        # the same --card clobber guard for free and no caller can rebuild
        # one without the other drifting.
        lpath = write_llms_txt(out, cards)
        print(f"  \u2713 llms.txt \u2192 {lpath} ({len(cards)} cards)")

    print(f"\nDone. {len(written)} card page(s) written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

