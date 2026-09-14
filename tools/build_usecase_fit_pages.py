"""Use-case fit pages: the bidirectional card -> guide link.

For every card, finds each use-case guide that RANKS it (card_id present in
guide["ranked"]) and writes out_dir/fit/<card_id>/index.html linking to each
of those guides. Framing is positional ("position N of M"), never a verdict —
this is the reverse direction of a /gear-for/<guide-id>/ ranking page, so a
card can point back at every guide it appears in without ever crowning it
"best" in one of them. A card that appears in no guide is skipped entirely.

Standalone module: does not import build_site, so it can run against any
`cards` / `guides` list shaped the way build_site.load_cards/load_guides
produce them, without depending on build_site's global BASE_URL or CSS.
"""
import html
from pathlib import Path

BANNED_PHRASES = ("best", "top", "#1", "winner", "worth it")


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def _card_display_name(card):
    return (card.get("identity", {}) or {}).get("display_name") or card.get("card_id", "")


def _guide_memberships(card_id, guides):
    """(guide, position, total) for every guide that ranks card_id, in the
    order `guides` was given. position is 1-based, total is len(ranked)."""
    memberships = []
    for guide in guides:
        ranked = guide.get("ranked") or []
        for i, row in enumerate(ranked):
            if row.get("card_id") == card_id:
                memberships.append((guide, i + 1, len(ranked)))
                break
    return memberships


def _render_card_page(card, memberships, base_url):
    cid = card["card_id"]
    name = _card_display_name(card)
    canonical = f"{base_url}/fit/{cid}/"
    items = "\n".join(
        f'    <li><a href="{base_url}/gear-for/{esc(guide.get("id"))}/">'
        f'{esc(guide.get("display_name") or guide.get("id"))}</a> '
        f'&mdash; appears in our sourced '
        f'{esc(guide.get("display_name") or guide.get("id"))} guide '
        f'(position {pos} of {total})</li>'
        for guide, pos, total in memberships
    )
    title = f"Where the {esc(name)} fits | AskMaddi"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<link rel="canonical" href="{esc(canonical)}">
</head>
<body>
<p><a href="/cards/{esc(cid)}/">&larr; Back to {esc(name)}</a></p>
<h1>Use-case fit: {esc(name)}</h1>
<p>Sourced use-case guides that {esc(name)} appears in:</p>
<ul>
{items}
</ul>
</body>
</html>
"""


def _render_hub_page(entries, base_url):
    canonical = f"{base_url}/fit/"
    rows = "\n".join(
        f'    <li><a href="/fit/{esc(card["card_id"])}/">'
        f'{esc(_card_display_name(card))}</a> '
        f'&mdash; appears in {len(memberships)} sourced guide'
        f'{"" if len(memberships) == 1 else "s"}</li>'
        for card, memberships in entries
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Use-case fit index | AskMaddi</title>
<link rel="canonical" href="{esc(canonical)}">
</head>
<body>
<h1>Use-case fit</h1>
<p>Every card and the sourced use-case guides it appears in.</p>
<ul>
{rows}
</ul>
</body>
</html>
"""


def build_pages(cards, guides, out_dir, base_url):
    """Write out_dir/fit/<card_id>/index.html for every card ranked by at
    least one guide, plus an out_dir/fit/index.html hub. Returns the list of
    written card-page paths (str), in card order, omitting skipped cards."""
    base_url = (base_url or "").rstrip("/")
    fit_dir = Path(out_dir) / "fit"
    written = []
    hub_entries = []
    for card in cards:
        cid = card.get("card_id")
        if not cid:
            continue
        memberships = _guide_memberships(cid, guides)
        if not memberships:
            continue
        page_dir = fit_dir / cid
        page_dir.mkdir(parents=True, exist_ok=True)
        page_path = page_dir / "index.html"
        page_path.write_text(_render_card_page(card, memberships, base_url),
                              encoding="utf-8")
        written.append(str(page_path))
        hub_entries.append((card, memberships))

    fit_dir.mkdir(parents=True, exist_ok=True)
    (fit_dir / "index.html").write_text(_render_hub_page(hub_entries, base_url),
                                         encoding="utf-8")
    return written
