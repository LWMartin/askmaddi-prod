#!/usr/bin/env python3
"""
build_vs_pages.py — deterministic '<A> vs <B>' comparison pages (maddi-
distribution v2.0 §8, unlocked once the catalogue passed the >=10-card gate).

A vs-page is a DETERMINISTIC JOIN of two published cards on their shared,
VISIBLE review axes. It never editorialises: every share stays grounded
(pos/neu/neg + denominator, the Phase-1 rule) and the page uses no verdict
vocabulary at all — it restates both cards' numbers side by side and lets the
reader decide.

Usage:
  python3 tools/build_vs_pages.py                       # data/cards -> browser/vs
  python3 tools/build_vs_pages.py --min-shared 3
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))

import build_site as B  # noqa: E402


DISCLOSURE_TEXT = ("Disclosure: We earn a commission when you buy through "
                    "links on this page, at no cost to you.")


def _visible_axes(card):
    """axis_id -> first visible axis, from lead_axes then detail_axes."""
    seen = {}
    for axis in (card.get('lead_axes') or []) + (card.get('detail_axes') or []):
        aid = axis.get('axis_id')
        if not aid or aid in seen:
            continue
        if not B._card_visible(axis):
            continue
        seen[aid] = axis
    return seen


def _sent_total(sent):
    total = sent.get('total')
    if total:
        return total
    return (sent.get('pos', 0) or 0) + (sent.get('neu', 0) or 0) + (sent.get('neg', 0) or 0)


def shared_axes(card_a, card_b):
    """The intersection of card_a's and card_b's visible axes, as comparison
    rows carrying both cards' sentiment triples. Ordered by combined claim
    volume descending, ties broken by axis_id ascending — a total order, so
    membership and sequence are both argument-symmetric."""
    axes_a = _visible_axes(card_a)
    axes_b = _visible_axes(card_b)
    shared_ids = set(axes_a) & set(axes_b)

    def combined_total(aid):
        return (_sent_total(axes_a[aid].get('sentiment') or {})
                + _sent_total(axes_b[aid].get('sentiment') or {}))

    ordered = sorted(shared_ids, key=lambda aid: (-combined_total(aid), aid))
    rows = []
    for aid in ordered:
        axis_a, axis_b = axes_a[aid], axes_b[aid]
        rows.append({
            'axis_id': aid,
            'display_name': axis_a.get('display_name') or axis_b.get('display_name') or aid,
            'a': axis_a.get('sentiment') or {},
            'b': axis_b.get('sentiment') or {},
        })
    return rows


def vs_slug(card_a, card_b):
    """Canonical, symmetric slug: sorted card_ids, so A-vs-B == B-vs-A."""
    lo, hi = sorted([card_a['card_id'], card_b['card_id']])
    return f'{lo}-vs-{hi}'


def _product_jsonld(card):
    ident = card.get('identity', {}) or {}
    obj = {
        '@type': 'Product',
        'name': ident.get('display_name', ''),
        'url': B.abs_url(f"/cards/{card['card_id']}/"),
    }
    if ident.get('brand'):
        obj['brand'] = {'@type': 'Brand', 'name': ident['brand']}
    return obj


def _cta_html(label, url):
    return (f'<a class="btn-affiliate" href="{B.esc(url)}" target="_blank" '
            f'rel="nofollow noopener sponsored">{B.esc(label)} →</a>')


def _axis_row_html(axis):
    total_a = _sent_total(axis['a'])
    total_b = _sent_total(axis['b'])
    return (
        '<tr class="vs-axis-row">'
        f'<th scope="row">{B.esc(axis["display_name"])}</th>'
        f'<td>{B.esc(B.sentiment_triple(axis["a"]))} of {total_a} claims</td>'
        f'<td>{B.esc(B.sentiment_triple(axis["b"]))} of {total_b} claims</td>'
        '</tr>'
    )


def render_vs_page(card_a, card_b, image_a=None, image_b=None):
    """A complete, byte-deterministic HTML page comparing two cards on their
    shared visible axes. No editorial verdict — restates both cards' grounded
    numbers side by side."""
    # Canonicalise orientation first so the page is identical whichever
    # argument order it is called with.
    if card_a['card_id'] > card_b['card_id']:
        card_a, card_b, image_a, image_b = card_b, card_a, image_b, image_a

    slug = vs_slug(card_a, card_b)
    canonical = B.abs_url(f'/vs/{slug}/')

    name_a = card_a['identity']['display_name']
    name_b = card_b['identity']['display_name']

    axes = shared_axes(card_a, card_b)
    rows_html = ''.join(_axis_row_html(a) for a in axes)

    new_label_a, new_url_a = B.new_cta(card_a)
    used_label_a, used_url_a = B.used_cta(card_a)
    new_label_b, new_url_b = B.new_cta(card_b)
    used_label_b, used_url_b = B.used_cta(card_b)

    year = B.analysis_year(card_a) or B.analysis_year(card_b) or ''
    asof = B.asof_phrase(card_a) or B.asof_phrase(card_b) or 'Analysis'

    jsonld_obj = {
        '@context': 'https://schema.org',
        '@type': 'ItemList',
        'itemListElement': [
            {'@type': 'ListItem', 'position': 1, 'item': _product_jsonld(card_a)},
            {'@type': 'ListItem', 'position': 2, 'item': _product_jsonld(card_b)},
        ],
    }
    jsonld = json.dumps(jsonld_obj, indent=2, ensure_ascii=False).replace('</', '<\\/')

    title = f'{name_a} vs {name_b}'
    meta_desc = (f'Side-by-side comparison of {name_a} and {name_b} across the '
                 f'review axes both products share, with grounded sentiment '
                 f'shares from our synthesized coverage.')

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{B.esc(title)} | AskMaddi</title>
  <meta name="description" content="{B.esc(meta_desc)}">
  <link rel="canonical" href="{B.esc(canonical)}">
  <meta property="og:title" content="{B.esc(title)} — AskMaddi">
  <meta property="og:description" content="{B.esc(meta_desc)}">
  <meta property="og:type" content="website">
  <meta property="og:url" content="{B.esc(canonical)}">
  <script type="application/ld+json">
{jsonld}
  </script>
  <link rel="icon" type="image/png" href="/images/logo.png">
  <link rel="stylesheet" href="/css/maddi.css">
</head>
<body>
  <div class="affiliate-disclosure-bar">{B.esc(DISCLOSURE_TEXT)}</div>
  <div class="container">

    <header class="header-compact">
      <a href="/" class="logo-title"><img src="/images/logo.png" alt="AskMaddi" class="site-logo">AskMaddi</a>
    </header>

    <article class="vs-page">
      <h1 class="vs-title">{B.esc(name_a)} vs {B.esc(name_b)}</h1>
      <p class="vs-intro">{B.esc(str(year))} · {B.esc(asof)} — a side-by-side restatement of the grounded review sentiment each product shares on the same axes. No axis appears here unless both products were rated on it.</p>

      <section class="vs-section vs-products">
        <div class="vs-product">
          <h2>{B.esc(name_a)}</h2>
          <div class="vs-cta-row">
            {_cta_html(new_label_a, new_url_a)}
            {_cta_html(used_label_a, used_url_a)}
          </div>
          <a class="vs-dossier-link" href="/cards/{B.esc(card_a['card_id'])}/">Full {B.esc(name_a)} review →</a>
        </div>
        <div class="vs-product">
          <h2>{B.esc(name_b)}</h2>
          <div class="vs-cta-row">
            {_cta_html(new_label_b, new_url_b)}
            {_cta_html(used_label_b, used_url_b)}
          </div>
          <a class="vs-dossier-link" href="/cards/{B.esc(card_b['card_id'])}/">Full {B.esc(name_b)} review →</a>
        </div>
      </section>

      <section class="vs-section vs-comparison">
        <h2 class="card-section-head">How {B.esc(name_a)} and {B.esc(name_b)} compare</h2>
        <table class="vs-axis-table">
          <thead>
            <tr><th scope="col">Axis</th><th scope="col">{B.esc(name_a)}</th><th scope="col">{B.esc(name_b)}</th></tr>
          </thead>
          <tbody>
            {rows_html}
          </tbody>
        </table>
      </section>
    </article>

    <footer class="card-footer">
      <a href="/">← Back to AskMaddi</a>
    </footer>

  </div>
</body>
</html>
"""


def select_pairs(cards, min_shared=2):
    """Deterministic pair selection: same non-empty category, at least
    min_shared shared axes, canonical orientation, list sorted by slug — so
    the output is identical regardless of input order and no A-vs-B/B-vs-A
    duplicate appears."""
    seen_slugs = set()
    pairs = []
    n = len(cards)
    for i in range(n):
        for j in range(i + 1, n):
            card_i, card_j = cards[i], cards[j]
            cat_i = (card_i.get('identity', {}) or {}).get('category') or ''
            cat_j = (card_j.get('identity', {}) or {}).get('category') or ''
            if not cat_i or cat_i != cat_j:
                continue
            if len(shared_axes(card_i, card_j)) < min_shared:
                continue
            lo, hi = ((card_i, card_j) if card_i['card_id'] <= card_j['card_id']
                      else (card_j, card_i))
            slug = vs_slug(lo, hi)
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            pairs.append((lo, hi))
    pairs.sort(key=lambda pair: vs_slug(*pair))
    return pairs


def _load_cards(cards_dir):
    cards = []
    for path in sorted(Path(cards_dir).glob('*.json')):
        try:
            cards.append(json.loads(path.read_text(encoding='utf-8')))
        except (json.JSONDecodeError, OSError):
            continue
    return cards


# ─── Curated selection (the seed file) ───────────────────────────────────────
# Which pairs to build is a HUMAN judgment, not an on-card heuristic: recon
# proved no on-card signal (category, subcategory, price adjacency, shared
# axes) reliably separates a real cross-shop from a coincidence — every
# heuristic leaks nonsense pairs (a telephoto zoom vs a wide prime; a
# point-and-shoot vs a mirrorless body). So the pairs live in a ratified seed
# file, data/vs_pairs.json. The comparator-fork fast-follow appends demand-true
# pairs to the SAME file (source='comparator_fork'), so this render/wire
# pipeline is built once and later fed automatically.
SEED_PATH = ROOT / 'data' / 'vs_pairs.json'


# Seed entries with these statuses are NOT built — they await the human ratify
# gate (review-is-the-craft-seat). A pair with no status builds (back-compat with
# external/test seeds); comparator_fork proposals land as 'proposed' and stay
# dark until a human flips them to 'live'.
_UNBUILT_STATUSES = {'proposed', 'proposed-crossbrand', 'hold', 'rejected'}


def load_seed(path=SEED_PATH):
    """Return the seed's buildable (a_id, b_id) tuples — those whose status is
    live or unset. Missing/malformed → []."""
    try:
        doc = json.loads(Path(path).read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return []
    out = []
    for entry in doc.get('pairs', []):
        a, b = entry.get('a'), entry.get('b')
        if a and b and a != b and entry.get('status') not in _UNBUILT_STATUSES:
            out.append((a, b))
    return out


def select_seeded_pairs(cards, seed, min_shared=1):
    """Resolve seed (a_id, b_id) tuples to card pairs. A pair is kept only when
    BOTH cards exist and share >= min_shared visible axes; the rest are reported
    as skips (so a stale seed entry surfaces loudly instead of silently
    vanishing). Canonical orientation + slug-sorted output — deterministic and
    duplicate-free, same guarantees as select_pairs."""
    by_id = {c['card_id']: c for c in cards}
    pairs, skipped, seen = [], [], set()
    for a_id, b_id in seed:
        ca, cb = by_id.get(a_id), by_id.get(b_id)
        if ca is None or cb is None:
            missing = [i for i, c in ((a_id, ca), (b_id, cb)) if c is None]
            skipped.append((a_id, b_id, f"missing card(s): {', '.join(missing)}"))
            continue
        n_shared = len(shared_axes(ca, cb))
        if n_shared < min_shared:
            skipped.append((a_id, b_id, f"only {n_shared} shared axis/axes"))
            continue
        lo, hi = (ca, cb) if ca['card_id'] <= cb['card_id'] else (cb, ca)
        slug = vs_slug(lo, hi)
        if slug in seen:
            continue
        seen.add(slug)
        pairs.append((lo, hi))
    pairs.sort(key=lambda pair: vs_slug(*pair))
    return pairs, skipped


def build_pages(cards, out_dir, seed_path=SEED_PATH, min_shared=2, fallback_auto=False):
    """Write vs-pages for the curated seed (if present) into out_dir. Returns
    (slugs_written, skipped). Falls back to the auto select_pairs primitive only
    when fallback_auto is set AND no usable seed exists — production always runs
    curated. Reusable as a library so build_site can call it in one build pass."""
    seed = load_seed(seed_path)
    skipped = []
    if seed:
        pairs, skipped = select_seeded_pairs(cards, seed, min_shared=1)
        mode = 'curated'
    elif fallback_auto:
        pairs = select_pairs(cards, min_shared=min_shared)
        mode = 'auto'
    else:
        pairs, mode = [], 'none'

    out = Path(out_dir)
    slugs = []
    for card_a, card_b in pairs:
        slug = vs_slug(card_a, card_b)
        page_dir = out / slug
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / 'index.html').write_text(render_vs_page(card_a, card_b), encoding='utf-8')
        slugs.append(slug)
    return slugs, skipped, mode


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build deterministic '<A> vs <B>' comparison pages from published card JSONs.")
    parser.add_argument('--cards-dir', default=str(ROOT / 'data' / 'cards'))
    parser.add_argument('--out', default=str(ROOT / 'browser' / 'vs'))
    parser.add_argument('--seed', default=str(SEED_PATH),
                        help="Curated pair seed (data/vs_pairs.json). Absent -> --auto or nothing.")
    parser.add_argument('--min-shared', type=int, default=2,
                        help="Auto-mode shared-axis floor (ignored in curated mode).")
    parser.add_argument('--auto', action='store_true',
                        help="Fall back to the N^2 same-category primitive when no seed exists.")
    args = parser.parse_args(argv)

    cards = _load_cards(args.cards_dir)
    slugs, skipped, mode = build_pages(
        cards, args.out, seed_path=args.seed,
        min_shared=args.min_shared, fallback_auto=args.auto)

    print(f'build_vs_pages [{mode}]: {len(cards)} card(s) loaded, '
          f'{len(slugs)} comparison page(s) written to {args.out}')
    for a_id, b_id, why in skipped:
        print(f'  ! skipped {a_id} vs {b_id}: {why}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
