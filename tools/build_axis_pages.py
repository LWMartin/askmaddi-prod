#!/usr/bin/env python3
"""Per-axis micro-pages — the "[product] [aspect]" long-tail surface.

One page per (card, axis) that reviewers actually discussed: /aspect/<card_id>/
<axis_id>/ — e.g. /aspect/sony-a7iv/video_capability/. Captures the enormous
"<product> autofocus", "<product> low light", "<product> video" query class.

★ EVIDENCE-GATED (the thin-content guard, learned from the RallyRates AdSense
"low value content" flag). A page is emitted ONLY where the axis carries real
sourced substance — >= MIN_SOURCES distinct sources AND >= MIN_MENTIONS total
mentions — so every page is a genuine multi-source synthesis, never a templated
stub. The thin tail (a lone mention) is SKIPPED, not published: a flood of
near-identical thin pages devalues the whole site, the opposite of the goal.

PRESENT-DON'T-RATE (locked). The page reports COUNTS ("65 positive · 25 mixed ·
17 critical across 14 sources") — counts are facts, the same discipline as the
on-card "N claims across N sources" and the C6 positiveNotes/negativeNotes.
NEVER a rating number, never "best". It leads with the card's curated face_quote
and shows a few top-weighted attributed excerpts, then points to the full card.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

MIN_SOURCES = 3
MIN_MENTIONS = 8
MAX_QUOTES = 3


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def abs_url(base_url, path):
    return base_url.rstrip("/") + path


def _display_name(card):
    return (card.get("identity", {}) or {}).get("display_name") or card.get("card_id", "")


def _axis_display(ax):
    return ax.get("display_name") or ax.get("axis_id", "").replace("_", " ").title()


def _reviewer_map(card):
    """source_id -> human reviewer byline, from the card's curated sources[] (C9);
    falls back to the source_type at call site."""
    out = {}
    for s in card.get("sources", []) or []:
        sid = s.get("source_id") or s.get("id")
        if sid:
            out[sid] = s.get("reviewer") or s.get("author") or s.get("name")
    return out


def _pick_quotes(sentiment, face_quote, reviewers):
    """Lead with the curated face_quote, then top-weighted DISTINCT-source
    excerpts. Dedups by source_id so one chatty reviewer can't fill the page."""
    rows = []
    seen = set()

    def _add(q):
        sid = q.get("source_id")
        text = (q.get("quote_excerpt") or "").strip()
        if not text or sid in seen:
            return
        seen.add(sid)
        by = reviewers.get(sid) or (q.get("source_type") or "review").replace("_", " ")
        rows.append({"text": text, "by": by, "url": q.get("url", ""),
                     "witness": q.get("witness")})

    if isinstance(face_quote, dict):
        _add(face_quote)
    for q in sorted((sentiment.get("sources") or []),
                    key=lambda s: s.get("weight") or 0, reverse=True):
        if len(rows) >= MAX_QUOTES:
            break
        _add(q)
    return rows[:MAX_QUOTES]


def _breadcrumb_jsonld(base_url, cid, product, aspect, canonical):
    # Home → Product → Aspect: every node is a REAL served page (no invented
    # category), so the trail is honest — SERP breadcrumbs + crawl context.
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home",
             "item": abs_url(base_url, "/")},
            {"@type": "ListItem", "position": 2, "name": product,
             "item": abs_url(base_url, f"/cards/{cid}/")},
            {"@type": "ListItem", "position": 3, "name": aspect, "item": canonical},
        ],
    }


def _jsonld(base_url, canonical, product, aspect, sent):
    # WebPage about the aspect + sourced COUNT notes (positive/negative), never a
    # rating (witness-stance). numberOfItems on the evidence, not a verdict.
    return {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "url": canonical,
        "name": f"{product}: {aspect} — what reviewers say",
        "about": {"@type": "Product", "name": product},
        "description": (f"{sent.get('pos', 0)} positive, {sent.get('neu', 0)} mixed, "
                        f"{sent.get('neg', 0)} critical mentions of {aspect.lower()} "
                        f"across {len(sent.get('sources') or [])} sources."),
    }


def render_axis_page(card, ax, base_url):
    cid = card["card_id"]
    aid = ax["axis_id"]
    product = _display_name(card)
    aspect = _axis_display(ax)
    sent = ax.get("sentiment") or {}
    pos, neu, neg = sent.get("pos", 0), sent.get("neu", 0), sent.get("neg", 0)
    nsrc = len(sent.get("sources") or [])
    canonical = abs_url(base_url, f"/aspect/{cid}/{aid}/")
    quotes = _pick_quotes(sent, ax.get("face_quote"), _reviewer_map(card))
    q_html = "\n".join(
        f'  <figure class="ax-q"><blockquote>{esc(q["text"])}</blockquote>'
        f'<figcaption>&mdash; {esc(q["by"])}'
        + (f' (<a href="{esc(q["url"])}" rel="nofollow">source</a>)' if q.get("url") else "")
        + "</figcaption></figure>"
        for q in quotes)
    jsonld = json.dumps(_jsonld(base_url, canonical, product, aspect, sent),
                        indent=2, ensure_ascii=False).replace("</", "<\\/")
    crumb = json.dumps(_breadcrumb_jsonld(base_url, cid, product, aspect, canonical),
                       indent=2, ensure_ascii=False).replace("</", "<\\/")
    title = f"{product} {aspect} — what reviewers say | AskMaddi"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(title)}</title>
<link rel="canonical" href="{esc(canonical)}">
<meta name="description" content="{esc(f'What reviewers say about the {product} {aspect.lower()}: {pos} positive, {neu} mixed, {neg} critical mentions across {nsrc} sources.')}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:url" content="{esc(canonical)}">
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:760px;margin:0 auto;padding:1.5rem;line-height:1.55}}
h1{{font-size:1.4rem}} .ax-counts{{color:#333;background:#f6f6f6;padding:.6rem .8rem;border-radius:6px;display:inline-block}}
.ax-counts b{{font-weight:600}} .ax-q{{margin:1rem 0;padding-left:1rem;border-left:3px solid #ddd}}
.ax-q blockquote{{margin:0;color:#222}} .ax-q figcaption{{color:#888;font-size:.85rem;margin-top:.3rem}}
.ax-more a{{font-weight:600}} .ax-note{{color:#888;font-size:.85rem;margin-top:1.5rem}}
</style>
<script type="application/ld+json">
{jsonld}
</script>
<script type="application/ld+json">
{crumb}
</script>
</head>
<body>
<p class="ax-crumb"><a href="/cards/{esc(cid)}/">{esc(product)}</a> &rsaquo; {esc(aspect)}</p>
<h1>{esc(product)}: {esc(aspect)}</h1>
<p class="ax-counts"><b>{pos}</b> positive &middot; <b>{neu}</b> mixed &middot; <b>{neg}</b> critical
mentions across <b>{nsrc}</b> sources.</p>
{q_html}
<p class="ax-more">Read the full sourced terrain: <a href="/cards/{esc(cid)}/">{esc(product)} review</a>.</p>
<p class="ax-note">Counts are drawn from the sources we track and reflect what reviewers said,
not a score &mdash; AskMaddi charts the terrain, it does not rate.</p>
</body>
</html>
"""


def _eligible_axes(card):
    for grp in ("lead_axes", "detail_axes"):
        for ax in (card.get(grp) or []):
            if not ax.get("axis_id"):
                continue
            sent = ax.get("sentiment") or {}
            if (len(sent.get("sources") or []) >= MIN_SOURCES
                    and (sent.get("total") or 0) >= MIN_MENTIONS):
                yield ax


def build_pages(cards, out_dir, base_url):
    """Write /aspect/<card_id>/<axis_id>/index.html for every card×axis that
    clears the evidence gate. Returns the list of page URLs written."""
    out = Path(out_dir)
    urls = []
    for card in cards:
        cid = card.get("card_id")
        if not cid:
            continue
        seen_axis = set()
        for ax in _eligible_axes(card):
            aid = ax["axis_id"]
            if aid in seen_axis:      # a lead+detail dup of the same axis
                continue
            seen_axis.add(aid)
            d = out / "aspect" / cid / aid
            d.mkdir(parents=True, exist_ok=True)
            (d / "index.html").write_text(render_axis_page(card, ax, base_url),
                                          encoding="utf-8")
            urls.append(f"{base_url.rstrip('/')}/aspect/{cid}/{aid}/")
    return urls


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Build per-axis micro-pages.")
    ap.add_argument("--cards-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--base-url", default="https://askmaddi.com")
    a = ap.parse_args()
    cards = [json.loads(p.read_text()) for p in Path(a.cards_dir).glob("*.json")]
    u = build_pages(cards, a.out_dir, a.base_url)
    print(f"wrote {len(u)} aspect page(s)")
