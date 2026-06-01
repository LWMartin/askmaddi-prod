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
from datetime import datetime, timezone
from pathlib import Path


# ─── Affiliate tags (match the live frontend / cards-manifest) ──────────────
AMAZON_TAG = "askmaddi-20"
EBAY_CAMPID = "5339138080"
EBAY_RID = "711-53200-19255-0"

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
def amazon_search_url(display_name):
    q = re.sub(r"\s+", "+", display_name.strip())
    return f"https://www.amazon.com/s?k={q}&tag={AMAZON_TAG}"


def ebay_search_url(display_name):
    q = re.sub(r"\s+", "+", display_name.strip().lower())
    return (
        f"https://www.ebay.com/sch/i.html?_nkw={q}"
        f"&mkcid=1&mkrid={EBAY_RID}&campid={EBAY_CAMPID}"
    )


def new_cta(card):
    """Return (label, url) for the 'buy new' CTA, honest about missing price."""
    pricing = card.get("pricing", {})
    name = card["identity"]["display_name"]
    url = pricing.get("affiliate_url") or pricing.get("current_new_url") or amazon_search_url(name)
    price = pricing.get("current_new_usd") or pricing.get("msrp_usd") or 0
    label = f"${int(price)} new" if price and price > 0 else "Check current price"
    return label, url


def used_cta(card):
    """Return (label, url) for the 'used' CTA, honest about missing price."""
    pricing = card.get("pricing", {})
    used = pricing.get("used_market", {}) or {}
    name = card["identity"]["display_name"]
    url = used.get("affiliate_url") or used.get("search_url") or ebay_search_url(name)
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

    # Top quote: first source ref that carries excerpt text, if any.
    # Real card schema uses `quote_excerpt`; reviewer is derived from source_id
    # when no explicit reviewer field is present.
    quote_html = ""
    for ref in (sent.get("sources") or []):
        q = ref.get("quote_excerpt") or ref.get("quote") or ref.get("text")
        if q:
            reviewer = ref.get("reviewer") or _reviewer_from_source_id(ref.get("source_id", ""))
            url = ref.get("url", "")
            attribution = (
                f' <a class="quote-cite" href="{esc(url)}" target="_blank" rel="nofollow noopener">\u2014 {esc(reviewer)}</a>'
                if url else f' <span class="quote-cite">\u2014 {esc(reviewer)}</span>'
            )
            quote_html = f'<blockquote class="axis-quote">\u201c{esc(q)}\u201d{attribution}</blockquote>'
            break

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
            <span class="axis-counts">{pos} pos \u00b7 {neu} neu \u00b7 {neg} neg</span>
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
    return f"""
      <section class="card-section">
        <h2 class="card-section-head">Used Market</h2>
        <div class="band-grid">{"".join(cells)}</div>
        {sold_line}
      </section>"""


# ─── Specs (product-forward; honest about absence) ──────────────────────────
# Specs are a facts-pipeline output (manufacturer-canonical key/value pairs).
# The card schema may not carry them yet — when absent or empty, the section
# is omitted entirely rather than rendered as an empty box (same discipline as
# pricing/issue_clusters). Numeric conflicts arrive pre-resolved as an honest
# spread string from the fact pipeline; we render the value verbatim.
def specs_section(card):
    specs = card.get("specs") or {}
    # Accept either a flat dict {label: value} or a list of {label, value}.
    rows = []
    if isinstance(specs, dict):
        items = specs.get("items", specs) if "items" in specs else specs
        if isinstance(items, dict):
            rows = [(k, v) for k, v in items.items() if v not in (None, "", [])]
        elif isinstance(items, list):
            rows = [(d.get("label", ""), d.get("value")) for d in items
                    if d.get("value") not in (None, "", [])]
    elif isinstance(specs, list):
        rows = [(d.get("label", ""), d.get("value")) for d in specs
                if d.get("value") not in (None, "", [])]
    if not rows:
        return ""
    cells = "".join(
        f'<div class="spec-row"><span class="spec-label">{esc(label)}</span>'
        f'<span class="spec-value">{esc(value)}</span></div>'
        for label, value in rows
    )
    return f"""
      <section class="card-section">
        <h2 class="card-section-head">Specifications</h2>
        <div class="spec-grid">{cells}</div>
      </section>"""


def _has_sentiment(axis):
    """True if an axis carries any rated sentiment. Axes with total==0 are
    empty (no reviewer touched them) and are suppressed from the page entirely
    rather than rendered as a 0/0/0 bar."""
    return ((axis.get("sentiment", {}) or {}).get("total", 0) or 0) > 0


# ─── Page assembly ──────────────────────────────────────────────────────────
def render_page(card, image_url=None):
    ident = card["identity"]
    name = ident["display_name"]
    brand = ident.get("brand", "")
    subcat = (ident.get("subcategory") or "").title()
    cat = (ident.get("category") or "").title()
    year = ident.get("year_introduced") or ""
    descriptor = " \u00b7 ".join([x for x in [brand, f"{subcat} {cat}".strip(), str(year)] if x])

    fresh = card.get("freshness", {}) or {}
    source_count = fresh.get("source_count", len(card.get("sources", [])))
    last_built = (fresh.get("last_built") or "")[:10]

    conf = (card.get("confidence", {}) or {}).get("overall", "unknown")

    new_label, new_url = new_cta(card)
    used_label, used_url = used_cta(card)

    synth = (card.get("synthesis", {}) or {}).get("consensus_paragraph", "")

    # Empty axes (sentiment.total == 0) are suppressed entirely — no reviewer
    # touched them, so rendering a 0/0/0 bar is noise, not information.
    lead = [a for a in (card.get("lead_axes", []) or []) if _has_sentiment(a)]
    detail = [a for a in (card.get("detail_axes", []) or []) if _has_sentiment(a)]

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

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{esc(name)} review — synthesized from {source_count} sources | AskMaddi</title>
  <meta name="description" content="{esc(meta_desc)}">
  <meta property="og:title" content="{esc(name)} — AskMaddi">
  <meta property="og:description" content="{esc(meta_desc)}">
  <meta property="og:type" content="product">
  {f'<meta property="og:image" content="{esc(img_url)}">' if img_url else ''}
  <link rel="icon" type="image/png" href="/images/logo.png">
  <link rel="stylesheet" href="/css/maddi.css">
  <link rel="stylesheet" href="/css/cards-detail.css">
</head>
<body>
  <div class="affiliate-disclosure-bar">Disclosure: We earn a commission when you buy through links on this page, at no cost to you.</div>
  <div class="container">

    <header class="header-compact">
      <a href="/" class="logo-title"><img src="/images/logo.png" alt="AskMaddi" class="site-logo">AskMaddi</a>
      <div class="search-box">
        <input type="text" id="detail-search-input" placeholder="Search another product\u2026">
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
            <a class="btn-affiliate btn-buy-new" href="{esc(new_url)}" target="_blank" rel="nofollow noopener sponsored">{esc(new_label)} \u2192</a>
            <a class="btn-affiliate btn-buy-used" href="{esc(used_url)}" target="_blank" rel="nofollow noopener sponsored">{esc(used_label)} \u2192</a>
          </div>
          <p class="hero-meta">
            Synthesized from <strong>{source_count}</strong> reviewer sources
            {f"\u00b7 Last updated {esc(last_built)}" if last_built else ""}
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
          <summary><h2 class="card-section-head inline">More detail \u2014 {len(detail)} additional axes</h2></summary>
          <div class="axes-stack">{detail_html}</div>
        </details>
      </section>''' if detail else ''}

      {price_html}

      <section class="card-section">
        <h2 class="card-section-head">Sources <span class="src-total">({source_count})</span></h2>
        <p class="src-intro">Every claim above traces to these original reviews. We don't write opinions \u2014 we synthesize theirs.</p>
        {sources_html}
      </section>

    </article>

    <footer class="card-footer">
      <a href="/">\u2190 Back to AskMaddi</a>
      <span>\u00b7</span>
      <a href="/mission.html">Our method</a>
    </footer>

  </div>
</body>
</html>
"""


# ─── Manifest (teaser grid) regeneration ────────────────────────────────────
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
    top = sorted(
        (card.get("lead_axes", []) or []),
        key=lambda a: (a.get("sentiment", {}) or {}).get("total", 0),
        reverse=True,
    )[:3]
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
            }
            for a in top
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
        cards.append(json.loads(Path(args.card).read_text()))
    if args.cards_dir:
        for p in sorted(Path(args.cards_dir).glob("*.json")):
            cards.append(json.loads(p.read_text()))
    return cards


def main():
    ap = argparse.ArgumentParser(description="Generate AskMaddi card detail pages.")
    ap.add_argument("--card", help="Path to a single card JSON.")
    ap.add_argument("--cards-dir", help="Directory of card JSONs (*.json).")
    ap.add_argument("--output-dir", default="browser", help="Output root (default: browser/).")
    ap.add_argument("--manifest", action="store_true", help="Also regenerate cards-manifest.json.")
    ap.add_argument("--image-url", help="Fallback product image URL when the card carries none "
                                        "(single-card builds only; card fields take precedence).")
    args = ap.parse_args()

    if not args.card and not args.cards_dir:
        ap.error("provide --card or --cards-dir")

    out = Path(args.output_dir)
    cards = load_cards(args)
    if not cards:
        print("No cards found.", file=sys.stderr)
        return 1

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

    print(f"\nDone. {len(written)} card page(s) written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
