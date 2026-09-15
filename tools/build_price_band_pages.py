#!/usr/bin/env python3
"""Price-banded lists — the "[X] under $Y" long-tail surface.

TWO families, both derived from data we already carry (pricing) — zero new data:

  category x band   /under/cameras-under-1000/     every body priced <= $1000
  guide x band      /under/cameras-for-wildlife-under-2000/
                    the wildlife guide's ranked roster, filtered to <= $2000,
                    IN GUIDE ORDER (the ranking is reused, not recomputed).

PRESENT-DON'T-RATE (locked, per maddi-witness-stance / positioning-terrain):
"under $X" is a FILTER, never a crown. A page LISTS what falls in the band, each
row carrying its ACTUAL price and the channel that price is from (new / open-box
/ used) — it never says "best". The category pages order by price ascending (a
neutral, useful order); the guide pages keep the guide's own fit order (already
evidence-traced, never a verdict). "best ... under $X" lives only in the page's
query_aliases / search intent, exactly like the use-case guides.

PRICE = the lowest REAL buyable price the card carries (min over current_new_usd,
used_market.bands.open_box, used_market.bands.pre_owned). A card with no price is
omitted, not guessed. Pages rebuild nightly with the used-price refresh, so the
bands stay honest as prices move (the row shows price_updated context via the
card link, and the band membership is recomputed every build).
"""
from __future__ import annotations

import html
import json
from pathlib import Path

# Cumulative ceilings: a $400 body appears on under-500 AND under-1000, the
# convention every "best under $X" list uses. Chosen off the live distribution
# (53/75/90/94 cards <= 500/1000/2000/3000) so no page is thin.
BANDS = (500, 1000, 2000, 3000)
GUIDE_BANDS = (1000, 2000, 3000)
# Only categories with enough priced cards to fill a band get a category page;
# the guide pages carry the rest (a guide is already a category-scoped roster).
CATEGORY_LABELS = {"body": "cameras", "lens": "lenses"}
MIN_CARDS = 3  # never emit a thin band page (no directory, no sitemap entry)


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def abs_url(base_url, path):
    return base_url.rstrip("/") + path


def _display_name(card):
    return (card.get("identity", {}) or {}).get("display_name") or card.get("card_id", "")


def card_price(card):
    """(price_float, channel_label) = the lowest real buyable price, or (None, None).
    channel ∈ {'new','open-box','used'} so the row can be honest about WHICH price
    puts the card in the band."""
    pr = card.get("pricing") or {}
    cands = []
    if pr.get("current_new_usd"):
        try:
            cands.append((float(pr["current_new_usd"]), "new"))
        except (TypeError, ValueError):
            pass
    bands = (pr.get("used_market") or {}).get("bands") or {}
    for key, label in (("open_box", "open-box"), ("pre_owned", "used")):
        if bands.get(key):
            try:
                cands.append((float(bands[key]), label))
            except (TypeError, ValueError):
                pass
    if not cands:
        return None, None
    return min(cands, key=lambda pc: pc[0])


def _money(v):
    return f"${v:,.0f}"


def _itemlist_jsonld(base_url, canonical, name, rows):
    return {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": name,
        "url": canonical,
        "numberOfItems": len(rows),
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1,
             "url": abs_url(base_url, f"/cards/{cid}/"), "name": nm}
            for i, (cid, nm, _price, _ch) in enumerate(rows)
        ],
    }


def _breadcrumb_jsonld(base_url, h1, canonical):
    # Home → Gear by budget (/under/) → this band. All real served pages.
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home",
             "item": abs_url(base_url, "/")},
            {"@type": "ListItem", "position": 2, "name": "Gear by budget",
             "item": abs_url(base_url, "/under/")},
            {"@type": "ListItem", "position": 3, "name": h1, "item": canonical},
        ],
    }


def _render(base_url, slug, title, h1, intro, rows, back=None):
    canonical = abs_url(base_url, f"/under/{slug}/")
    jsonld = json.dumps(_itemlist_jsonld(base_url, canonical, h1, rows),
                        indent=2, ensure_ascii=False).replace("</", "<\\/")
    crumb = json.dumps(_breadcrumb_jsonld(base_url, h1, canonical),
                       indent=2, ensure_ascii=False).replace("</", "<\\/")
    items = "\n".join(
        f'    <li><a href="{esc(abs_url(base_url, f"/cards/{cid}/"))}">{esc(nm)}</a>'
        f' <span class="pb-price">{esc(_money(price))} <em>{esc(ch)}</em></span></li>'
        for cid, nm, price, ch in rows
    )
    back_html = (f'<p class="pb-back"><a href="{esc(back[0])}">&larr; {esc(back[1])}</a></p>'
                 if back else "")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(title)}</title>
<link rel="canonical" href="{esc(canonical)}">
<meta name="description" content="{esc(intro)}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:url" content="{esc(canonical)}">
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:820px;margin:0 auto;padding:1.5rem;line-height:1.5}}
h1{{font-size:1.5rem}} .pb-intro{{color:#444}}
ul.pb-list{{list-style:none;padding:0}} ul.pb-list li{{padding:.5rem 0;border-bottom:1px solid #eee;display:flex;justify-content:space-between;gap:1rem}}
.pb-price{{color:#333;white-space:nowrap}} .pb-price em{{color:#888;font-style:normal;font-size:.85em}}
.pb-note{{color:#888;font-size:.85rem;margin-top:1.5rem}}
</style>
<script type="application/ld+json">
{jsonld}
</script>
<script type="application/ld+json">
{crumb}
</script>
</head>
<body>
{back_html}
<h1>{esc(h1)}</h1>
<p class="pb-intro">{esc(intro)}</p>
<ul class="pb-list">
{items}
</ul>
<p class="pb-note">Prices are the lowest currently-tracked buyable price (new, open-box, or
used) and refresh nightly &mdash; a listing here reports fit and price, not a ranking. Follow any product for
its full sourced review terrain.</p>
</body>
</html>
"""


def build_pages(cards, guides, out_dir, base_url):
    """Write /under/<slug>/index.html for every populated (category|guide) x band.
    Returns the list of page URLs written (for the sitemap)."""
    out = Path(out_dir)
    urls = []
    hub_entries = []  # (url_path, label, count)

    priced = []
    price_by_id = {}
    for c in cards:
        p, ch = card_price(c)
        if p is None:
            continue
        priced.append(c)
        price_by_id[c["card_id"]] = (p, ch)

    def _write(slug, title, h1, intro, rows, back=None):
        d = out / "under" / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text(_render(base_url, slug, title, h1, intro, rows, back),
                                      encoding="utf-8")
        urls.append(f"{base_url.rstrip('/')}/under/{slug}/")
        hub_entries.append((f"/under/{slug}/", h1, len(rows)))

    # ── category x band ───────────────────────────────────────────────
    for cat, label in CATEGORY_LABELS.items():
        pool = [c for c in priced if (c.get("identity", {}) or {}).get("category") == cat]
        for band in BANDS:
            rows = sorted(
                ((c["card_id"], _display_name(c), *price_by_id[c["card_id"]])
                 for c in pool if price_by_id[c["card_id"]][0] <= band),
                key=lambda r: r[2])
            if len(rows) < MIN_CARDS:
                continue
            cap = label.capitalize()
            _write(f"{label}-under-{band}",
                   f"{cap} under {_money(band)} — {len(rows)} tracked | AskMaddi",
                   f"{cap} under {_money(band)}",
                   f"Every {label[:-1]} we track with a current buyable price at or below "
                   f"{_money(band)}, newest prices first-refreshed nightly.",
                   rows)

    # ── guide x band ──────────────────────────────────────────────────
    for guide in (guides or []):
        gid = guide.get("id")
        if not gid:
            continue
        disp = guide.get("display_name") or gid
        applies = guide.get("applies_to") or []
        noun = "lenses" if applies == ["lens"] else "cameras" if applies == ["body"] else "gear"
        ranked = guide.get("ranked") or []
        for band in GUIDE_BANDS:
            rows = [(r["card_id"], _display_name_from_rank(r), *price_by_id[r["card_id"]])
                    for r in ranked
                    if r.get("card_id") in price_by_id
                    and price_by_id[r["card_id"]][0] <= band]
            if len(rows) < MIN_CARDS:
                continue
            _write(f"{gid}-under-{band}",
                   f"{disp} {noun} under {_money(band)} — {len(rows)} ranked | AskMaddi",
                   f"{disp} {noun} under {_money(band)}",
                   f"Our sourced {disp} {noun} guide, filtered to models with a buyable "
                   f"price at or below {_money(band)} — in the guide's evidence order.",
                   rows,
                   back=(abs_url(base_url, f"/gear-for/{gid}/"),
                         f"Full {disp} {noun} guide"))

    # ── hub /under/ ───────────────────────────────────────────────────
    if hub_entries:
        cat_rows = [e for e in hub_entries if "-for-" not in e[0]]
        guide_rows = [e for e in hub_entries if "-for-" in e[0]]
        def _links(entries):
            return "\n".join(
                f'    <li><a href="{esc(p)}">{esc(lbl)}</a> '
                f'<span class="pb-price">{n} tracked</span></li>'
                for p, lbl, n in entries)
        canonical = abs_url(base_url, "/under/")
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Gear by price — under $X | AskMaddi</title>
<link rel="canonical" href="{esc(canonical)}">
<meta name="description" content="Browse cameras and lenses by budget: what we track under each price ceiling, and our use-case guides filtered to your budget.">
<style>body{{font-family:system-ui,sans-serif;max-width:820px;margin:0 auto;padding:1.5rem;line-height:1.5}}
h1{{font-size:1.5rem}}h2{{font-size:1.1rem;margin-top:1.5rem}}ul{{list-style:none;padding:0}}
li{{padding:.4rem 0;border-bottom:1px solid #eee;display:flex;justify-content:space-between}}
.pb-price{{color:#888;font-size:.85em}}</style></head>
<body>
<h1>Gear by budget</h1>
<p>What we currently track under each price ceiling &mdash; prices refresh nightly.
A budget list reports fit and price; it is not a ranking.</p>
<h2>By category</h2>
<ul>
{_links(cat_rows)}
</ul>
<h2>By use-case, within budget</h2>
<ul>
{_links(guide_rows)}
</ul>
</body></html>
"""
        (out / "under").mkdir(parents=True, exist_ok=True)
        (out / "under" / "index.html").write_text(body, encoding="utf-8")
        urls.append(f"{base_url.rstrip('/')}/under/")

    return urls


def _display_name_from_rank(rank_row):
    return rank_row.get("display_name") or rank_row.get("card_id", "")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Build price-banded list pages.")
    ap.add_argument("--cards-dir", required=True)
    ap.add_argument("--guides-dir")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--base-url", default="https://askmaddi.com")
    a = ap.parse_args()
    cards = [json.loads(p.read_text()) for p in Path(a.cards_dir).glob("*.json")]
    guides = ([json.loads(p.read_text()) for p in Path(a.guides_dir).glob("*.json")]
              if a.guides_dir else [])
    u = build_pages(cards, guides, a.out_dir, a.base_url)
    print(f"wrote {len(u)} price-band page(s)")
