#!/usr/bin/env python3
"""
build_divergence_pages.py — "Where reviewers push back" divergence surface.

Standalone generator (does not modify build_site.py) that emits, per card:
  browser/where-they-split/<card_id>/index.html

plus one hub:
  browser/where-they-split/index.html

Voice discipline: report, don't rate. Every page states what reviewers
disagreed about or criticized, sourced and attributed — never a verdict.
The words 'best', 'top', '#1', 'winner', 'worth it' must never appear in any
rendered output. A card with nothing honest to report (no issue clusters and
no axis reviewers meaningfully split on) is skipped outright, not padded into
a thin page.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_site import esc, card_name, _source_reviewer_map, _reviewer_from_source_id, SITE_NAME  # noqa: E402

# polarity_agreement < this = the aggregator's own "contested" cutoff (verified
# against data/cards/*.json: contested=True iff polarity_agreement <= 0.596,
# contested=False iff polarity_agreement >= 0.6 — a clean boundary at 0.6).
CONVERGENCE_SPLIT_THRESHOLD = 0.6
MAX_SPLIT_AXES = 3

BANNED_WORDS = ("best", "top", "#1", "winner", "worth it")

_PAGE_CSS = """
.split-hero{padding:1.5rem 0 .5rem}
.split-hero h1{margin:0 0 .4rem}
.split-intro{color:var(--color-text-secondary,#555);max-width:62ch;line-height:1.55}
.split-section{margin:1.75rem 0}
.split-section h2{font-size:1.1rem;margin-bottom:.75rem}
.crit-item{margin:0 0 1.1rem;padding-bottom:1.1rem;border-bottom:1px solid var(--color-border-light,#f0ece8)}
.crit-item:last-child{border-bottom:none}
.crit-label{font-weight:650}
.crit-count{color:var(--color-text-muted,#9a938c);font-size:.85rem;margin-left:.4rem}
.crit-quote{margin:.4rem 0 0;padding-left:.9rem;border-left:3px solid var(--color-border,#e5e0db);font-style:italic;color:var(--color-text-secondary,#555)}
.crit-cite{display:block;margin:.25rem 0 0;font-style:normal;font-size:.85rem;color:var(--color-text-muted,#9a938c)}
.split-axis-list{list-style:none;margin:0;padding:0}
.split-axis-list li{padding:.4rem 0}
.split-backlinks{margin:.2rem 0 .6rem;font-size:.9rem}
.split-backlinks a{color:var(--color-primary-dark,#c4623f);text-decoration:none}
.split-backlinks a:hover{text-decoration:underline}
.split-hub-cat{margin:1.5rem 0 2rem}
.split-hub-cat-head{font-size:15px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:var(--color-text-muted,#9a938c);margin-bottom:.75rem}
.split-hub-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:var(--space-md,16px)}
.split-hub-tile{display:block;background:var(--color-surface,#fff);border:1px solid var(--color-border-light,#f0ece8);border-radius:var(--radius-lg,16px);padding:var(--space-lg,24px);text-decoration:none;color:inherit}
.split-hub-tile:hover{border-color:var(--color-border,#e5e0db)}
"""


def _convergence_value(axis):
    """Numeric polarity-agreement reading from axis.convergence, or None.

    Handles both the real aggregator shape (a dict carrying polarity_agreement
    / contested) and a plain float, which callers may pass directly."""
    conv = axis.get("convergence")
    if isinstance(conv, dict):
        pa = conv.get("polarity_agreement")
        return float(pa) if isinstance(pa, (int, float)) else None
    if isinstance(conv, (int, float)):
        return float(conv)
    return None


def _is_low_convergence(axis):
    """True if reviewers meaningfully split on this axis.

    Prefers the aggregator's own 'contested' flag when present (it is the
    authoritative version of this same cutoff); falls back to the threshold
    for plain-float convergence values."""
    conv = axis.get("convergence")
    if isinstance(conv, dict) and conv.get("contested") is not None:
        return bool(conv["contested"])
    val = _convergence_value(axis)
    return val is not None and val < CONVERGENCE_SPLIT_THRESHOLD


def _combined_axes(card):
    """lead_axes + detail_axes, deduped by axis_id (lead wins), in order."""
    seen = {}
    for axis in list(card.get("lead_axes") or []) + list(card.get("detail_axes") or []):
        aid = axis.get("axis_id")
        if aid and aid not in seen:
            seen[aid] = axis
    return list(seen.values())


def _split_axes(card):
    """Axes reviewers split on, most-contested first, capped for readability."""
    axes = [a for a in _combined_axes(card) if _is_low_convergence(a)]
    axes.sort(key=lambda a: (_convergence_value(a) if _convergence_value(a) is not None else 1.0,
                              a.get("axis_id") or ""))
    return axes[:MAX_SPLIT_AXES]


def _most_debated_axis(card):
    """The axis object named by axis_roles.lowest_rated, or None."""
    aid = (card.get("axis_roles") or {}).get("lowest_rated")
    if not aid:
        return None
    for axis in _combined_axes(card):
        if axis.get("axis_id") == aid:
            return axis
    return None


def _cluster_quote(cluster):
    """(quote_text, source_id) — ONE attributed excerpt for a cluster, or
    (None, None) if the cluster carries nothing quotable."""
    sid = cluster.get("source_id")
    q = cluster.get("quote") or cluster.get("quote_excerpt")
    if q:
        return q, sid
    for key in ("quotes", "source_quotes"):
        items = cluster.get(key) or []
        if items:
            first = items[0]
            if isinstance(first, dict):
                return (first.get("quote") or first.get("quote_excerpt") or "",
                        first.get("source_id") or sid)
            if isinstance(first, str):
                return first, sid
    return None, None


def _contains_banned(text):
    low = (text or "").lower()
    return any(w in low for w in BANNED_WORDS)


def _attribute(source_id, source_map):
    """Reviewer name for a source_id — never AskMaddi, degrades to a neutral
    generic label when no source_id is available at all."""
    if not source_id:
        return "a reviewer"
    return source_map.get(source_id) or _reviewer_from_source_id(source_id)


def _issue_clusters(card):
    return (card.get("synthesis", {}) or {}).get("issue_clusters", []) or []


def _card_qualifies(card):
    return bool(_issue_clusters(card)) or bool(_split_axes(card))


def _criticisms_html(card, source_map):
    clusters = _issue_clusters(card)
    if not clusters:
        return ""
    items = []
    for c in clusters:
        label = c.get("label", c.get("aspect", ""))
        if not label:
            continue
        count = c.get("count", c.get("citations", 0))
        quote, source_id = _cluster_quote(c)
        quote_html = ""
        if quote and not _contains_banned(quote):
            reviewer = esc(_attribute(source_id, source_map))
            quote_html = (f'<blockquote class="crit-quote">“{esc(quote)}”'
                          f'<cite class="crit-cite">— {reviewer}</cite></blockquote>')
        items.append(
            f'<div class="crit-item"><span class="crit-label">{esc(label)}</span>'
            f'<span class="crit-count">({esc(count)} sourced mentions)</span>'
            f'{quote_html}</div>')
    if not items:
        return ""
    return (f'<section class="split-section"><h2>Common criticisms</h2>'
            f'{"".join(items)}</section>')


def _split_section_html(card):
    axes = _split_axes(card)
    if not axes:
        return ""
    items = []
    for a in axes:
        name = a.get("display_name") or a.get("axis_id") or ""
        val = _convergence_value(a)
        pct_note = f' <span class="crit-count">(~{round(val * 100)}% agreement)</span>' if val is not None else ""
        items.append(f'<li>{esc(name)}{pct_note}</li>')
    return (f'<section class="split-section"><h2>Where reviewers split</h2>'
            f'<ul class="split-axis-list">{"".join(items)}</ul></section>')


def _most_debated_html(card):
    axis = _most_debated_axis(card)
    if not axis:
        return ""
    name = axis.get("display_name") or axis.get("axis_id") or ""
    return (f'<section class="split-section"><h2>Most-debated aspect</h2>'
            f'<p>Across the sourced coverage, reviewers are most critical of '
            f'{esc(name)}.</p></section>')


def _card_jsonld(card, canonical, base_url):
    name = card_name(card)
    doc = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": f"Where reviewers push back on {name}",
        "url": canonical,
        "mainEntityOfPage": canonical,
        "about": {"@type": "Product", "name": name,
                  "url": f"{base_url}/cards/{card['card_id']}/"},
        "isPartOf": {"@type": "WebSite", "name": SITE_NAME, "url": base_url + "/"},
    }
    return json.dumps(doc, indent=2)


def _render_card_page(card, base_url):
    cid = card["card_id"]
    name = card_name(card) or cid
    canonical = f"{base_url}/where-they-split/{cid}/"
    sources = card.get("sources", []) or []
    source_map = _source_reviewer_map(sources)

    criticisms_html = _criticisms_html(card, source_map)
    split_html = _split_section_html(card)
    debated_html = _most_debated_html(card)
    body_sections = "\n".join(s for s in (criticisms_html, split_html, debated_html) if s)

    title = f"Where reviewers push back on the {esc(name)} | {SITE_NAME}"
    meta_desc = (f"Where independent reviewers disagreed or pushed back on the "
                 f"{name} — sourced criticisms and the axes coverage split on, "
                 f"attributed to the reviewers who said them.")
    jsonld = _card_jsonld(card, canonical, base_url)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <meta name="description" content="{esc(meta_desc)}">
  <link rel="canonical" href="{esc(canonical)}">
  <meta property="og:title" content="Where reviewers push back on the {esc(name)}">
  <meta property="og:description" content="{esc(meta_desc)}">
  <meta property="og:type" content="article">
  <meta property="og:url" content="{esc(canonical)}">
  <meta property="og:site_name" content="{SITE_NAME}">
  <script type="application/ld+json">
{jsonld}
  </script>
  <link rel="icon" type="image/png" href="/images/logo.png">
  <link rel="stylesheet" href="/css/maddi.css">
  <link rel="stylesheet" href="/css/cards-detail.css">
  <style>{_PAGE_CSS}</style>
</head>
<body data-page="where-they-split-card">
  <div class="container">
    <header class="header-compact">
      <a href="/" class="logo-title"><img src="/images/logo.png" alt="{SITE_NAME}" class="site-logo">{SITE_NAME}</a>
    </header>

    <article class="card-detail">
      <p class="split-backlinks">
        <a href="/cards/{esc(cid)}/">← Full {esc(name)} report</a>
        <span> · </span>
        <a href="/where-they-split/">All divergence reports</a>
      </p>
      <section class="split-hero">
        <h1>Where reviewers push back on the {esc(name)}</h1>
        <p class="split-intro">Independent reviewers do not always agree. This page collects
        where coverage of the {esc(name)} diverged or criticized it — sourced and
        attributed, not scored.</p>
      </section>
      {body_sections}
    </article>

    <footer class="card-footer">
      <a href="/cards/{esc(cid)}/">← Full {esc(name)} report</a>
      <span>·</span>
      <a href="/where-they-split/">All divergence reports</a>
    </footer>
  </div>
</body>
</html>
"""
    return html, canonical


def _hub_jsonld(entries, base_url):
    items = [
        {"@type": "ListItem", "position": i + 1,
         "url": f"{base_url}/where-they-split/{c['card_id']}/",
         "name": card_name(c)}
        for i, (c, _canonical) in enumerate(entries)
    ]
    doc = {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": "Where reviewers push back",
        "url": f"{base_url}/where-they-split/",
        "isPartOf": {"@type": "WebSite", "name": SITE_NAME, "url": base_url + "/"},
        "mainEntity": {"@type": "ItemList", "numberOfItems": len(items),
                       "itemListElement": items},
    }
    return json.dumps(doc, indent=2)


def _render_hub(entries, base_url):
    canonical = f"{base_url}/where-they-split/"
    n = len(entries)

    groups = {}
    for card, _canonical in entries:
        cat = (card.get("identity", {}) or {}).get("category") or ""
        groups.setdefault(cat, []).append(card)
    for cat in groups:
        groups[cat].sort(key=lambda c: (card_name(c).lower(), c["card_id"]))

    def _cat_label(cat):
        return cat.replace("_", " ").title() if cat else "Other Gear"

    ordered_cats = sorted(groups.keys(), key=lambda c: (c == "", _cat_label(c).lower()))

    sections = []
    for cat in ordered_cats:
        cards_in_cat = groups[cat]
        tiles = "".join(
            f'<a class="split-hub-tile" href="/where-they-split/{esc(c["card_id"])}/">'
            f'{esc(card_name(c) or c["card_id"])}</a>'
            for c in cards_in_cat)
        sections.append(
            f'<section class="split-hub-cat">'
            f'<h2 class="split-hub-cat-head">{esc(_cat_label(cat))} ({len(cards_in_cat)})</h2>'
            f'<div class="split-hub-grid">{tiles}</div></section>')
    sections_html = "\n".join(sections)

    title = f"Where reviewers push back — {n} divergence reports | {SITE_NAME}"
    meta_desc = (f"Every AskMaddi product where independent reviewers pushed back or "
                 f"split — {n} sourced divergence reports, grouped by category.")
    jsonld = _hub_jsonld(entries, base_url)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{esc(title)}</title>
  <meta name="description" content="{esc(meta_desc)}">
  <link rel="canonical" href="{esc(canonical)}">
  <meta property="og:title" content="Where reviewers push back — {SITE_NAME}">
  <meta property="og:description" content="{esc(meta_desc)}">
  <meta property="og:type" content="website">
  <meta property="og:url" content="{esc(canonical)}">
  <meta property="og:site_name" content="{SITE_NAME}">
  <script type="application/ld+json">
{jsonld}
  </script>
  <link rel="icon" type="image/png" href="/images/logo.png">
  <link rel="stylesheet" href="/css/maddi.css">
  <link rel="stylesheet" href="/css/cards-detail.css">
  <style>{_PAGE_CSS}</style>
</head>
<body data-page="where-they-split-hub">
  <div class="container">
    <header class="header-compact">
      <a href="/" class="logo-title"><img src="/images/logo.png" alt="{SITE_NAME}" class="site-logo">{SITE_NAME}</a>
    </header>

    <article class="card-detail">
      <section class="split-hero">
        <h1>Where reviewers push back</h1>
        <p class="split-intro">Independent reviewers do not always agree. These reports
        collect the sourced criticisms and axes coverage split on for each product —
        attributed, not scored.</p>
      </section>
      {sections_html}
    </article>

    <footer class="card-footer">
      <a href="/">← Back to {SITE_NAME}</a>
    </footer>
  </div>
</body>
</html>
"""


def build_pages(cards, out_dir, base_url):
    """Write every qualifying card's divergence page plus the hub.

    Returns the list of generated card-page canonical URLs (input order), for
    the caller's sitemap splice. Cards with neither issue_clusters nor a
    reviewer-split axis are skipped: there is nothing honest to report."""
    base_url = (base_url or "").rstrip("/")
    out = Path(out_dir)
    entries = []
    for card in cards:
        if not _card_qualifies(card):
            continue
        html, canonical = _render_card_page(card, base_url)
        page_dir = out / "where-they-split" / card["card_id"]
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / "index.html").write_text(html, encoding="utf-8")
        entries.append((card, canonical))

    hub_dir = out / "where-they-split"
    hub_dir.mkdir(parents=True, exist_ok=True)
    (hub_dir / "index.html").write_text(_render_hub(entries, base_url), encoding="utf-8")

    return [canonical for _card, canonical in entries]
