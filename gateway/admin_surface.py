"""
AskMaddi Gateway — Admin Review Surface (Phase 4)
=================================================
The human end of the write-back loop. The cron/capture path enqueues
slug-ambiguous resolved products into review_queue (status=pending); this
surface is where Lee *sees* each proposed card and adjudicates it into the
skus.json spine (promote) or routes it back upstream as a pipeline-bug
signal (reject).

Design contract
---------------
- ONE gate definition. promote()/reject() live in review_queue; this module is
  a thin HTML+form boundary over them. The page and its POST handlers call the
  SAME functions — no parallel promotion logic, nothing that can drift from the
  collision gate. A human override that still collides is a hard reject here too,
  surfaced as an inline banner, never a silent bypass and never a 500.
- The render is part of the review, not decoration. Each pending record is shown
  as a card preview built from the FROZEN identity captured at enqueue time
  (image, market_title, brand/MPN, price_seen) so the judgment is made against
  what the card will actually look like — alongside the review metadata that
  drives the decision: reason badge, proposed_slug, and (for collisions) the
  spine slug it clashed with.
- Spine-writing is the highest-privilege action in the system, so unlike the
  proxy-trusted read routes this surface REQUIRES an admin secret on every
  request (GET render and both POSTs), constant-time compared.

Auth
----
Single ADMIN_TOKEN env secret (loaded via app_production's .env loader or
systemd EnvironmentFile), used as the HTTP Basic-auth PASSWORD under a fixed
ADMIN_USER. If unset, the surface refuses to serve at all (503) — fail closed,
never an open spine-writer. The browser holds the credential and re-sends the
Authorization header on every request, so the secret never enters the URL,
browser history, or a bookmark. Compared with hmac.compare_digest. This is a
sole-operator tool behind the Apache proxy, not a multi-user auth system; the
bar is "no unauthenticated spine write," met by a required, constant-time-checked
shared secret the browser carries out of band.

Registration
------------
app_production imports register_admin(app) under its HAS_CAPTURE guard (the
review_queue import the admin surface depends on is the same one capture uses).
If review_queue is unavailable the surface is simply not registered.
"""

import hmac
import json
import os
import subprocess
import sys
import html as _html
from pathlib import Path

from flask import request, Response

import review_queue

# work_queue is the card-factory build-lifecycle store (Piece 4). The Review
# Ready section reads load_by_state('review_ready') for built cards awaiting the
# publish gate, load_by_state('failed') for the pipeline-health panel, counts()
# for the cockpit, and drives mark_published / reject_card. Guarded like the rest
# so a gateway without the factory store still serves the slug-review surface.
try:
    import work_queue
    _HAS_WORK_QUEUE = True
except ImportError:
    work_queue = None
    _HAS_WORK_QUEUE = False

# skus_registry is the IDENTITY + PROVENANCE spine. The Review Ready section
# joins each review_ready record's slug back to its spine entry to read the mint
# provenance (source, minted_needs_review, category) — three orthogonal facts
# about one slug, each read from its authoritative home (build state from
# work_queue, content from the assembled card.json, provenance from the spine).
# A review_ready card whose slug is ABSENT from the spine is publish-disabled
# (option 2): visible, with the reason shown, but barred from going live without
# an identity behind it.
try:
    import skus_registry
    _HAS_SKUS = True
except ImportError:
    skus_registry = None
    _HAS_SKUS = False

# ebay_api is needed only by the /admin/reresolve correction loop (a real
# resolve() round-trip when a human picks a different listing). Guarded: if the
# gateway is missing eBay creds/module, the rest of /admin still serves and
# re-resolve fails visibly (503-style banner) rather than 500ing.
try:
    import ebay_api
    _HAS_EBAY = True
except ImportError:
    ebay_api = None
    _HAS_EBAY = False


# --- Publish render runner ----------------------------------------------
#
# Publishing a review_ready card means rendering it LIVE: build_site.py reads the
# assembled card.json and emits browser/cards/{card_id}/index.html (+ refreshes
# the teaser manifest). That render is the one human-approved touch the factory
# deliberately stops short of (card_factory stops at 'assemble'). It's a
# subprocess that needs the repo filesystem, so — same discipline as the
# factory's injected build runner — the route takes an INJECTED callable. The
# real default shells out to build_site; tests pass a fake so the publish gate's
# state/auth/provenance logic is exercised with no filesystem render.
#
# A render runner is callable(card_path) -> (rc, detail): rc 0 == card is live.

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUILD_SITE = _REPO_ROOT / 'tools' / 'build_site.py'
_BROWSER_OUT = _REPO_ROOT / 'browser'
_CARDS_DIR = _REPO_ROOT / 'data' / 'cards'


def build_site_runner(build_site_path=_BUILD_SITE, output_dir=_BROWSER_OUT,
                      cards_dir=_CARDS_DIR, python=None):
    """Produce the PRODUCTION publish runner: callable(card_path) -> (rc, detail).

    PUBLISH MEANS JOINING THE CORPUS, NOT REPLACING IT (found live 2026-07-03,
    the gate's FIRST real publish): build_site's `--manifest` regenerates
    cards-manifest.json from only the cards loaded THAT run — correct for
    `--cards-dir` full rebuilds, destructive composed with `--card`: the first
    single-card publish shrank the homepage grid to one card (detail pages
    survived; the manifest is whole-file). The durable shape:

      1. Admit the approved card into data/cards/<card_id>.json — the
         canonical published corpus (atomic tmp+replace; republish = clean
         overwrite, naturally idempotent).
      2. Rebuild from `--cards-dir data/cards/` with `--manifest --sitemap` —
         the grid always reflects the full corpus, and every publish
         re-renders all detail pages, propagating the nightly's freshened
         prices/ASIN registry to the whole site as a side effect.

    Returns (returncode, detail): rc 0 means the card is live; non-zero
    carries the stderr tail for the publish banner. A card missing card_id
    fails closed BEFORE touching the corpus.
    """
    python = python or sys.executable
    build_site_path = Path(build_site_path)
    cards_dir = Path(cards_dir)

    def _run(card_path):
        try:
            card = json.loads(Path(card_path).read_text(encoding='utf-8'))
            card_id = card['card_id']
        except (OSError, ValueError, KeyError) as e:
            return 1, f'card unreadable or missing card_id: {e}'

        cards_dir.mkdir(parents=True, exist_ok=True)
        dest = cards_dir / f'{card_id}.json'
        tmp = cards_dir / f'.{card_id}.json.tmp'
        try:
            tmp.write_text(json.dumps(card, indent=2, ensure_ascii=False) + '\n',
                           encoding='utf-8')
            os.replace(tmp, dest)
        except OSError as e:
            if tmp.exists():
                tmp.unlink()
            return 1, f'corpus admit failed: {e}'

        cmd = [
            python, str(build_site_path),
            '--cards-dir', str(cards_dir),
            '--output-dir', str(output_dir),
            '--manifest',
            '--sitemap',
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            return 0, 'ok'
        tail = (proc.stderr or proc.stdout or '').strip().splitlines()
        return proc.returncode, (tail[-1] if tail else f'exit {proc.returncode}')

    return _run


# --- Auth ---------------------------------------------------------------
#
# HTTP Basic auth. The ADMIN_TOKEN env secret is the PASSWORD; the username is
# fixed to ADMIN_USER ('admin'). Chosen over a ?token= query param because the
# secret then never enters the URL bar, browser history, or a bookmark — the
# browser holds it in its session credential store and re-sends the header on
# every request. Structural, not a "remember not to bookmark this" promise.
# Same fail-closed posture: no ADMIN_TOKEN configured => the surface refuses to
# serve at all (503), never an open spine writer.

ADMIN_USER = 'admin'


def _admin_token():
    """The configured admin secret (the Basic-auth password), or '' if unset
    (=> surface fails closed)."""
    return os.environ.get('ADMIN_TOKEN', '')


def _authed():
    """Constant-time check of the Basic-auth credentials against ADMIN_USER /
    ADMIN_TOKEN. False if no secret is configured (fail closed) or no/!match
    credentials supplied."""
    configured = _admin_token()
    if not configured:
        return False
    auth = request.authorization
    if not auth or auth.type != 'basic':
        return False
    user_ok = hmac.compare_digest(auth.username or '', ADMIN_USER)
    pass_ok = hmac.compare_digest(auth.password or '', configured)
    # Evaluate both halves regardless of the first, so timing doesn't leak which
    # field was wrong.
    return user_ok and pass_ok


def _challenge():
    """401 with a WWW-Authenticate header so the browser shows its native
    credential prompt."""
    return Response(
        'unauthorized', status=401,
        headers={'WWW-Authenticate': 'Basic realm="AskMaddi Admin"'})


# --- Render helpers -----------------------------------------------------

def _esc(value):
    """HTML-escape any value for safe interpolation into the page."""
    return _html.escape('' if value is None else str(value))


def _price_line(identity):
    """'$1437.00 USD as of 2026-06-25' from the frozen price_seen block."""
    ps = (identity or {}).get('price_seen', {}) or {}
    value = ps.get('value', '')
    currency = ps.get('currency', '')
    as_of = (ps.get('as_of', '') or '')[:10]
    if not value:
        return 'no price captured'
    line = f"{value} {currency}".strip()
    if as_of:
        line += f" — as of {as_of}"
    return line


def _candidates_block(record):
    """Render the ranked eBay candidates for a low_resolve_confidence record.

    The resolve-time analog of the 2026-06-17 near-miss sidecar: surface what the
    disambiguator weighed so a human catches an OVERLOOKED product — a listing the
    machine passed over that is actually the right one. The chosen row is marked;
    every row carries a one-click "re-resolve to this listing" button that re-freezes
    the record's identity onto that candidate (via a real eBay round-trip) and leaves
    it pending for the normal collision-gated promote. Renders nothing for records
    without candidates (collision/needs_review), so the existing surface is unchanged.
    """
    candidates = record.get('candidates') or []
    if not candidates:
        return ''
    qid = record.get('queue_id', '')

    rows = []
    for c in candidates:
        chosen = c.get('chosen')
        human = c.get('human_chosen')
        item_id = c.get('item_id', '')
        price = f"{_esc(c.get('price',''))} {_esc(c.get('currency',''))}".strip()
        cond = _esc(c.get('condition', ''))
        score = c.get('score')
        if human:
            mark = '<span class="pick human">your pick</span>'
        elif chosen:
            score_txt = f"{score:.0%}" if isinstance(score, (int, float)) else ''
            mark = f'<span class="pick machine">machine pick · {score_txt}</span>'
        else:
            mark = ''
        # One-click re-resolve. Disabled (no button) for the row already chosen —
        # re-resolving to the current pick is a no-op round-trip.
        action = '' if chosen else (
            f'<form class="reresolve" method="post" action="/admin/reresolve">'
            f'<input type="hidden" name="queue_id" value="{_esc(qid)}">'
            f'<input type="hidden" name="item_id" value="{_esc(item_id)}">'
            f'<button type="submit" class="pickbtn">use this listing</button>'
            f'</form>'
        )
        rows.append(
            f'<tr class="{"chosen" if chosen else ""}">'
            f'<td class="ctitle">{_esc(c.get("title",""))}</td>'
            f'<td class="cprice">{price}</td>'
            f'<td class="ccond">{cond}</td>'
            f'<td class="cmark">{mark}</td>'
            f'<td class="cact">{action}</td>'
            f'</tr>'
        )

    return (
        '<div class="candidates">'
        '<p class="clegend">The factory was UNSURE which listing is this product. '
        'Below is what it weighed — scan for an OVERLOOKED listing the machine '
        'passed over. If the machine pick is wrong, choose the right listing; '
        'identity re-freezes from eBay and you still promote through the slug gate. '
        'If none fits, reject as <code>not_the_product</code> (a disambiguator bug).</p>'
        '<table class="candtable"><thead><tr>'
        '<th>listing title</th><th>price</th><th>cond</th><th>pick</th><th></th>'
        '</tr></thead><tbody>'
        + ''.join(rows) +
        '</tbody></table></div>'
    )


def _reason_badge(record):
    """The decision driver: collision (clash with an existing spine slug),
    needs_review (ambiguous normalization, no direct clash), or
    low_resolve_confidence (slug resolved clean but the demand factory's eBay-pick
    was uncertain — the human picks the right listing from `candidates`)."""
    reason = record.get('reason', '')
    if reason == 'collision':
        return ('collision', record.get('collision_with') or '?')
    if reason == 'low_resolve_confidence':
        chosen = next((c for c in (record.get('candidates') or [])
                       if c.get('chosen')), None)
        conf = chosen.get('score') if chosen else None
        detail = f"machine pick {conf:.0%} confident" if isinstance(conf, (int, float)) else ''
        return ('low resolve confidence', detail)
    return ('needs review', '')


def _card_html(record):
    """One pending record rendered as a card preview + adjudication forms.

    Built entirely from stored record fields — no eBay re-fetch, no invented
    data. The promote form's slug field is pre-filled with proposed_slug (a
    starting point Lee can override); the reject form is a controlled-vocab
    dropdown because a reject is a structured pipeline-bug signal.
    """
    ident = record.get('identity', {}) or {}
    qid = record.get('queue_id', '')
    image = ident.get('image', '')
    title = ident.get('market_title', '') or record.get('input_text', '')
    brand = ident.get('brand', '')
    mpn = ident.get('mpn', '')
    epid = ident.get('epid', '')
    badge_label, badge_detail = _reason_badge(record)
    proposed = record.get('proposed_slug', '')

    brand_mpn = ' · '.join(p for p in (brand, mpn) if p)
    collision_block = ''
    if badge_label == 'collision':
        collision_block = (
            '<div class="collision">clashes with spine slug '
            f'<code>{_esc(badge_detail)}</code> — a promote slug that still '
            'normalizes the same will be hard-rejected</div>'
        )

    img_html = (
        f'<img class="thumb" src="{_esc(image)}" alt="" loading="lazy">'
        if image else '<div class="thumb noimg">no image</div>'
    )

    reason_opts = ''.join(
        f'<option value="{_esc(r)}">{_esc(r)}</option>'
        for r in review_queue.REJECT_REASONS
    )

    return f"""
    <article class="card">
      <div class="preview">
        {img_html}
        <div class="meta">
          <h2>{_esc(title) or '<em>untitled</em>'}</h2>
          <div class="sub">{_esc(brand_mpn)}</div>
          <div class="price">{_esc(_price_line(ident))}</div>
          <div class="ids">
            <span>{_esc(record.get('vendor'))} / {_esc(record.get('model'))}</span>
            <span class="cat">{_esc(record.get('category'))}</span>
            {f'<span class="epid">epid {_esc(epid)}</span>' if epid else ''}
          </div>
        </div>
      </div>
      <div class="adjudication">
        <div class="badge {badge_label.replace(' ', '-')}">{_esc(badge_label)}</div>
        {collision_block}
        {_candidates_block(record)}
        <form class="promote" method="post" action="/admin/promote">
          <input type="hidden" name="queue_id" value="{_esc(qid)}">
          <label>authorize slug
            <input type="text" name="override_slug" value="{_esc(proposed)}"
                   spellcheck="false" autocomplete="off">
          </label>
          <button type="submit" class="go">Promote to spine</button>
        </form>
        <form class="reject" method="post" action="/admin/reject">
          <input type="hidden" name="queue_id" value="{_esc(qid)}">
          <label>reject reason
            <select name="reason">{reason_opts}</select>
          </label>
          <button type="submit" class="no">Reject (upstream bug)</button>
        </form>
      </div>
    </article>
    """


# ========================================================================
# Piece 4 — Review Ready section (the card-factory PUBLISH gate)
# ========================================================================
#
# The slug-review surface above adjudicates AMBIGUOUS identity into the spine
# (review_queue.promote writes a slug — an identity decision). This section is a
# different gate in kind: a clean-resolved/minted card the factory has already
# BUILT (work_queue state review_ready), awaiting the human's one in-the-loop
# touch — render it live, yes/no (work_queue.mark_published — a publish decision).
#
# Three orthogonal facts about one slug converge here for that decision, each
# read from its authoritative home:
#   - BUILD state   work_queue.load_by_state('review_ready')  (the list itself)
#   - CONTENT       the assembled card.json at record['card_path']  (the preview)
#   - PROVENANCE    the spine entry skus.json[slug]  (source / minted_needs_review)
# This is not three copies of one fact; it's the one place all three meet.


def _load_card(card_path):
    """Load an assembled card.json from a work_queue record's card_path.

    Returns the parsed dict, or None if the path is missing/unreadable/corrupt —
    a built card whose artifact we can't read is publish-disabled (option 2),
    surfaced with the reason, never a 500 and never silently published.
    """
    if not card_path:
        return None
    try:
        return json.loads(Path(card_path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _spine_entry(slug):
    """The spine (skus.json) entry for a slug, or None if absent / spine module
    unavailable. The provenance join: build state lives in work_queue, identity
    and provenance live here. A review_ready slug missing from the spine is the
    publish-disabled integrity case (option 2)."""
    if not _HAS_SKUS:
        return None
    try:
        return skus_registry.load_registry().get('skus', {}).get(slug)
    except (OSError, ValueError):
        return None


def _provenance_trace(entry):
    """Render the machine-mint provenance trace from a spine entry.

    The robust treatment (Lee, 2026-06-30): not just a badge but a visible note
    pulling the mint specifics, so a foul card is traceable to its mint at a
    glance. A 'resolved' entry (hand-curated / tapped slug) gets a quiet trusted
    marker; a 'generated' (machine-minted) entry gets a loud badge PLUS the
    trace: source, the eBay-derived category, and — the spec's explicit 'look
    harder' trigger — a flag when category fell back to '' on an unknown eBay
    category id. The four frozen entries lack the provenance fields entirely, so
    .get() reads them as the trusted default ('resolved' / False).
    """
    source = entry.get('source', 'resolved')
    needs_review = entry.get('minted_needs_review', False)
    category = skus_registry.get_facet(entry) or '' if skus_registry else ''
    cat_id = (skus_registry.get_marketplace_category(entry) or ''
              if skus_registry else '')

    if source != 'generated' and not needs_review:
        # Hand-curated / tapped identity — the trusted historical path.
        return ('<div class="prov trusted">'
                '<span class="provbadge ok">curated identity</span>'
                '</div>')

    # Machine-minted: badge + the trace that makes a foul card traceable.
    rows = [
        f'<li><b>source</b>: {_esc(source)} '
        '<span class="provhint">(slug minted by resolve_slug from '
        'demand-discovered vendor/model)</span></li>',
        f'<li><b>eBay category id</b>: {_esc(cat_id) or "—"}</li>',
    ]
    # The spec's loud "look harder" trigger: category empty on a mint means the
    # eBay category id didn't map to controlled vocab — the card has no category.
    if not category:
        rows.append(
            '<li class="provalert"><b>category fell back to empty</b> — the eBay '
            'category id did not map to controlled vocab; this card has NO '
            'category. Verify the product type before publishing.</li>')
    else:
        rows.append(f'<li><b>category</b>: {_esc(category)} '
                    '<span class="provhint">(derived from eBay, not hand-tagged)'
                    '</span></li>')

    return (
        '<div class="prov minted">'
        '<span class="provbadge minted">machine-minted · look harder</span>'
        '<ul class="provtrace">' + ''.join(rows) + '</ul>'
        '</div>'
    )


def _ready_card_html(record):
    """One review_ready work_queue record rendered as a publish-gate card.

    Preview built from the assembled card.json (CONTENT) joined to the spine
    entry (PROVENANCE). If the card.json is unreadable OR the slug is missing
    from the spine, the card renders publish-DISABLED with the reason shown —
    visible so Lee sees what's stuck and why, but barred from going live without
    a readable artifact and an identity behind it (option 2). Reject stays
    available regardless: a card you can't publish you can still decline.
    """
    slug = record.get('slug', '')
    card = _load_card(record.get('card_path'))
    entry = _spine_entry(slug)

    # Gate the publish action: need both a readable card AND a spine entry.
    blockers = []
    if card is None:
        blockers.append('assembled card.json is missing or unreadable')
    if entry is None:
        blockers.append('no spine entry — identity/provenance unavailable')
    publishable = not blockers

    ident = (card or {}).get('identity', {}) or {}
    title = (ident.get('display_name')
             or record.get('label') or slug or '<untitled>')
    brand_model = ' · '.join(
        p for p in (ident.get('brand', ''), ident.get('model', '')) if p)
    cat_line = ' / '.join(
        p for p in (ident.get('category', ''), ident.get('subcategory', '')) if p)
    image = ident.get('image_thumb', '')
    image_source = ident.get('image_source', '')
    # Live override state from the SPINE (the card.json bakes the state at
    # assemble time; the spine shows what the NEXT assemble will read).
    spine_override_img = ((entry or {}).get('overrides') or {}).get('image_thumb', '')

    pricing = (card or {}).get('pricing', {}) or {}
    new_usd = pricing.get('current_new_usd') or 0
    price_line = (f'${new_usd:,.2f}' if new_usd
                  else 'no live price — “check current price” CTA on card')

    fresh = (card or {}).get('freshness', {}) or {}
    src_count = fresh.get('source_count', 0)
    build_model = fresh.get('build_model', '')
    conf = ((card or {}).get('confidence', {}) or {}).get('overall', '')

    img_html = (
        f'<img class="thumb" src="{_esc(image)}" alt="{_esc(title)}" loading="lazy">'
        if image else '<div class="thumb noimg">no image</div>')
    # Image provenance badge (images-on-spine step 6): where the pick came
    # from — 'ebay_catalog' (stock shot), 'ebay_listing' (seller photo, the
    # one-glance "camera on a carpet" check), 'manual' (hand-pasted).
    if image_source:
        img_html += (f'<span class="imgsrc {_esc(image_source)}">'
                     f'{_esc(image_source)}</span>')
    pending_note = ''
    if spine_override_img and spine_override_img != image:
        pending_note = ('<div class="imgpending">override on spine differs '
                        'from this build — re-assemble to bake it in</div>')
    override_form = (
        '<form class="imgoverride" method="post" action="/admin/set-override">'
        f'<input type="hidden" name="slug" value="{_esc(slug)}">'
        '<input type="hidden" name="field" value="image_thumb">'
        '<input type="url" name="value" placeholder="paste replacement image URL" '
        f'value="{_esc(spine_override_img)}">'
        '<button type="submit" class="go small">Set image override</button>'
        '</form>')

    prov_html = _provenance_trace(entry) if entry is not None else (
        '<div class="prov missing">'
        '<span class="provbadge alert">no provenance</span>'
        '<ul class="provtrace"><li class="provalert">'
        'slug absent from the spine — cannot verify identity or mint origin'
        '</li></ul></div>')

    blocker_html = ''
    if blockers:
        items = ''.join(f'<li>{_esc(b)}</li>' for b in blockers)
        blocker_html = (
            f'<div class="blocked">Publish disabled: <ul>{items}</ul>'
            'Fix the integrity issue (rebuild the card / restore the spine '
            'entry) — or reject if the build is genuinely bad.</div>')

    if publishable:
        publish_form = (
            '<form class="promote" method="post" action="/admin/publish">'
            f'<input type="hidden" name="slug" value="{_esc(slug)}">'
            '<button type="submit" class="go">Publish live</button>'
            '</form>')
    else:
        publish_form = ('<button type="button" class="go disabled" disabled '
                        'title="resolve the integrity blocker first">'
                        'Publish live</button>')

    reject_opts = ''.join(
        f'<option value="{_esc(r)}">{_esc(r)}</option>'
        for r in (work_queue.CARD_REJECT_REASONS if _HAS_WORK_QUEUE else ()))

    return f"""
    <article class="card ready">
      <div class="preview">
        {img_html}
        <div class="meta">
          <h2>{_esc(title)}</h2>
          <div class="sub">{_esc(brand_model)}</div>
          <div class="price">{_esc(price_line)}</div>
          <div class="ids">
            <span class="cat">{_esc(cat_line)}</span>
            <span>{_esc(src_count)} sources</span>
            {f'<span>conf: {_esc(conf)}</span>' if conf else ''}
            {f'<span class="bm">{_esc(build_model)}</span>' if build_model else ''}
          </div>
        </div>
      </div>
      <div class="adjudication">
        {prov_html}
        {pending_note}
        {override_form}
        {blocker_html}
        {publish_form}
        <form class="reject" method="post" action="/admin/reject-card">
          <input type="hidden" name="slug" value="{_esc(slug)}">
          <label>reject reason
            <select name="reason">{reject_opts}</select>
          </label>
          <button type="submit" class="no">Reject (pipeline bug)</button>
        </form>
      </div>
    </article>
    """


def _cockpit_html():
    """The factory-health header: work_queue.counts() as queue depths + cap.

    One glance at the build pipeline — how many cards are resolved-and-waiting,
    building, ready to publish, already promoted, or failed, plus today's build
    count against the cap. Reads straight off the store the design note
    earmarked for this. Renders nothing if work_queue is unavailable.
    """
    if not _HAS_WORK_QUEUE:
        return ''
    try:
        c = work_queue.counts()
    except (OSError, ValueError):
        return ''
    cells = [
        ('resolved', c.get('resolved', 0)),
        ('building', c.get('building', 0)),
        ('review ready', c.get('review_ready', 0)),
        ('promoted', c.get('promoted', 0)),
        ('failed', c.get('failed', 0)),
    ]
    stat_html = ''.join(
        f'<div class="stat"><span class="n">{_esc(n)}</span>'
        f'<span class="k">{_esc(k)}</span></div>'
        for k, n in cells)
    built = c.get('built_today', 0)
    return (
        '<section class="cockpit">'
        f'{stat_html}'
        f'<div class="stat cap"><span class="n">{_esc(built)}</span>'
        '<span class="k">built today</span></div>'
        '</section>')


def _failed_panel_html():
    """Collapsed pipeline-health panel: work_queue.load_by_state('failed').

    Distinct from the rejected cards — a `failed` record is a build that CRASHED
    (build_card.py exhausted retries), a mechanical pipeline-health signal, not a
    content-quality judgment. Collapsed by default (it's a diagnostic, not the
    daily flow); each row shows slug, attempts, and the last error tail.
    """
    if not _HAS_WORK_QUEUE:
        return ''
    try:
        failed = work_queue.load_by_state('failed')
    except (OSError, ValueError):
        return ''
    if not failed:
        return ''
    rows = ''.join(
        f'<tr><td class="fslug">{_esc(r.get("slug",""))}</td>'
        f'<td class="fatt">{_esc(r.get("build_attempts",0))}</td>'
        f'<td class="ferr">{_esc(r.get("last_error","") or "")}</td></tr>'
        for r in failed)
    return (
        f'<details class="failed-panel"><summary>{len(failed)} failed build(s) '
        '— pipeline health (build crashes, not rejects)</summary>'
        '<table class="ftable"><thead><tr><th>slug</th><th>attempts</th>'
        '<th>last error</th></tr></thead><tbody>'
        f'{rows}</tbody></table></details>')


def _review_ready_section_html():
    """The Review Ready section: cockpit + built cards awaiting publish.

    Leads the page (the daily publish flow — what Lee touches most). Renders the
    cockpit header, then one publish-gate card per review_ready record, then the
    collapsed failed panel. Empty state is explicit so an empty factory reads as
    'nothing to publish', not a broken page.
    """
    if not _HAS_WORK_QUEUE:
        return ''
    try:
        ready = work_queue.load_by_state('review_ready')
    except (OSError, ValueError):
        ready = []
    cockpit = _cockpit_html()
    if ready:
        cards = ''.join(_ready_card_html(r) for r in ready)
    else:
        cards = ('<div class="empty">No cards awaiting publish — the factory has '
                 'nothing review-ready right now.</div>')
    return (
        '<section class="ready-section">'
        '<h2 class="section-h">Review Ready '
        '<span class="section-sub">built cards awaiting the publish gate</span></h2>'
        f'{cockpit}{cards}{_failed_panel_html()}'
        '</section>')


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AskMaddi — Review Queue</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.5 system-ui, sans-serif; margin: 0; padding: 24px;
         max-width: 880px; margin-inline: auto; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .lead {{ opacity: .65; margin: 0 0 20px; }}
  .banner {{ padding: 10px 14px; border-radius: 8px; margin-bottom: 18px;
            font-weight: 500; }}
  .banner.ok {{ background: #e6f4ea; color: #14532d; }}
  .banner.err {{ background: #fde7e7; color: #7f1d1d; }}
  .empty {{ opacity: .6; padding: 40px 0; text-align: center; }}
  .card {{ border: 1px solid color-mix(in srgb, currentColor 18%, transparent);
          border-radius: 12px; padding: 16px; margin-bottom: 16px; }}
  .preview {{ display: flex; gap: 16px; }}
  .thumb {{ width: 96px; height: 96px; object-fit: contain; border-radius: 8px;
           background: color-mix(in srgb, currentColor 6%, transparent);
           flex: 0 0 auto; }}
  .thumb.noimg {{ display: grid; place-items: center; font-size: 11px;
                 opacity: .5; }}
  .imgsrc {{ display: inline-block; font-size: 10px; padding: 1px 6px;
            border-radius: 6px; margin-top: 4px; letter-spacing: .02em; }}
  .imgsrc.ebay_catalog {{ background: #e6f4ea; color: #14532d; }}
  .imgsrc.ebay_listing {{ background: #fef3c7; color: #713f12; }}
  .imgsrc.manual {{ background: #e0e7ff; color: #312e81; }}
  .imgpending {{ font-size: 12px; padding: 6px 10px; border-radius: 8px;
                background: #fef3c7; color: #713f12; margin-bottom: 6px; }}
  .imgoverride {{ display: flex; gap: 6px; margin-bottom: 8px; }}
  .imgoverride input[type=url] {{ flex: 1; font-size: 12px; padding: 4px 8px;
                border-radius: 6px; border: 1px solid
                color-mix(in srgb, currentColor 25%, transparent); }}
  .go.small {{ font-size: 12px; padding: 4px 10px; }}
  .meta h2 {{ font-size: 16px; margin: 0 0 2px; }}
  .sub {{ opacity: .7; font-size: 13px; }}
  .price {{ margin-top: 4px; font-variant-numeric: tabular-nums; }}
  .ids {{ margin-top: 6px; font-size: 12px; opacity: .6; display: flex;
         gap: 10px; flex-wrap: wrap; }}
  .cat {{ text-transform: uppercase; letter-spacing: .04em; }}
  .adjudication {{ margin-top: 14px; padding-top: 12px;
                  border-top: 1px solid color-mix(in srgb, currentColor 12%, transparent);
                  display: flex; gap: 12px; align-items: flex-end; flex-wrap: wrap; }}
  .badge {{ font-size: 11px; font-weight: 700; text-transform: uppercase;
           letter-spacing: .05em; padding: 3px 8px; border-radius: 999px; }}
  .badge.collision {{ background: #fde7e7; color: #7f1d1d; }}
  .badge.needs-review {{ background: #fef3c7; color: #78350f; }}
  .collision {{ flex-basis: 100%; font-size: 13px; color: #7f1d1d; }}
  .collision code, .ids code {{ font-family: ui-monospace, monospace; }}
  form {{ display: flex; gap: 8px; align-items: flex-end; }}
  label {{ display: flex; flex-direction: column; gap: 3px; font-size: 12px;
          opacity: .8; }}
  input[type=text], select {{ font: inherit; padding: 6px 8px; border-radius: 6px;
          border: 1px solid color-mix(in srgb, currentColor 30%, transparent);
          background: transparent; color: inherit; }}
  input[type=text] {{ width: 220px; font-family: ui-monospace, monospace; }}
  button {{ font: inherit; padding: 7px 12px; border-radius: 6px; border: 0;
           cursor: pointer; font-weight: 600; }}
  button.go {{ background: #15803d; color: #fff; }}
  button.no {{ background: transparent; color: #b91c1c;
              border: 1px solid currentColor; }}
  button.go.disabled {{ background: color-mix(in srgb, currentColor 20%, transparent);
              color: color-mix(in srgb, currentColor 50%, transparent);
              cursor: not-allowed; }}

  /* --- Piece 4: Review Ready section --- */
  .section-h {{ font-size: 16px; margin: 28px 0 12px; display: flex;
              align-items: baseline; gap: 10px; }}
  .section-h.slug {{ margin-top: 40px; padding-top: 20px;
              border-top: 2px solid color-mix(in srgb, currentColor 14%, transparent); }}
  .section-sub {{ font-size: 12px; font-weight: 400; opacity: .55; }}
  .cockpit {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }}
  .cockpit .stat {{ display: flex; flex-direction: column; align-items: center;
              padding: 8px 14px; border-radius: 8px; min-width: 64px;
              background: color-mix(in srgb, currentColor 5%, transparent); }}
  .cockpit .stat .n {{ font-size: 20px; font-weight: 700;
              font-variant-numeric: tabular-nums; }}
  .cockpit .stat .k {{ font-size: 10px; text-transform: uppercase;
              letter-spacing: .05em; opacity: .6; }}
  .cockpit .stat.cap {{ background: color-mix(in srgb, #15803d 12%, transparent); }}
  .card.ready {{ border-left: 3px solid #15803d; }}
  .bm {{ font-family: ui-monospace, monospace; opacity: .5; }}
  .prov {{ flex-basis: 100%; }}
  .provbadge {{ font-size: 11px; font-weight: 700; text-transform: uppercase;
              letter-spacing: .05em; padding: 3px 8px; border-radius: 999px; }}
  .provbadge.ok {{ background: color-mix(in srgb, #15803d 14%, transparent);
              color: #15803d; }}
  .provbadge.minted {{ background: #fef3c7; color: #78350f; }}
  .provbadge.alert {{ background: #fde7e7; color: #7f1d1d; }}
  .provtrace {{ margin: 8px 0 0; padding-left: 18px; font-size: 12px;
              opacity: .85; }}
  .provtrace li {{ margin: 2px 0; }}
  .provhint {{ opacity: .55; }}
  .provalert {{ color: #7f1d1d; font-weight: 600; }}
  .blocked {{ flex-basis: 100%; font-size: 13px; color: #7f1d1d;
              background: #fde7e7; padding: 10px 12px; border-radius: 8px; }}
  .blocked ul {{ margin: 4px 0; }}
  .failed-panel {{ margin-top: 24px; font-size: 13px; }}
  .failed-panel summary {{ cursor: pointer; opacity: .7; }}
  .ftable {{ width: 100%; border-collapse: collapse; margin-top: 10px;
              font-size: 12px; }}
  .ftable th, .ftable td {{ text-align: left; padding: 4px 8px;
              border-bottom: 1px solid color-mix(in srgb, currentColor 10%, transparent);
              vertical-align: top; }}
  .ftable .fslug {{ font-family: ui-monospace, monospace; }}
  .ftable .ferr {{ opacity: .7; }}
  .candtable {{ width: 100%; border-collapse: collapse; margin: 8px 0;
              font-size: 12px; }}
  .card.conflictcard {{ border-left: 3px solid #b91c1c; }}
  .gtinchip {{ font-family: ui-monospace, monospace; font-size: 12px;
              padding: 2px 6px; border-radius: 6px;
              background: color-mix(in srgb, #b91c1c 10%, transparent); }}
</style></head>
<body>
  {body}
</body></html>"""


_SLUG_SECTION_HEADER = (
    '<h2 class="section-h slug">Review Queue '
    '<span class="section-sub">{count} pending · adjudicate ambiguous identity '
    'into the skus.json spine or reject upstream</span></h2>')


_GTIN_SECTION_HEADER = (
    '<h2 class="section-h slug">GTIN Conflicts '
    '<span class="section-sub">{count} receipt(s) · Axis A abstains — '
    'evidence preserved, human judgment pending</span></h2>')


# --- GTIN conflict receipts (substrate-5b, read-only this pass) ----------

def _gtin_conflicts():
    """Spine entries carrying an unresolved GTIN conflict receipt.

    A conflict receipt is `identity.gtin_provenance.conflict == True` with
    `identity.gtin` still null — the persisted output of either layer's
    abstain path (L1: the listing's own observations disagree; L2: the
    admission gate's CONFLICT_DROP, >=2 catalog candidates on distinct GTINs).
    An entry whose gtin is later set (set_gtin is upgrade-only, so that write
    IS the adjudication) drops out of this filter naturally — resolution
    semantics without a status field. A DISMISSED receipt (gtin stays null,
    human ruled no assignable GTIN) drops out on its appended adjudication
    event — the append-only terminal marker, same discipline.

    Defensive by construction: a malformed provenance block (non-dict, missing
    keys) is skipped, never a 500 — this is a render of receipts, not a
    validator of them.
    """
    if skus_registry is None:
        return []
    try:
        skus = skus_registry.load_registry().get('skus', {})
    except (OSError, ValueError):
        return []
    out = []
    for slug, entry in sorted(skus.items()):
        identity = entry.get('identity') if isinstance(entry, dict) else None
        if not isinstance(identity, dict) or skus_registry.get_gtin(entry):
            continue
        prov = identity.get('gtin_provenance')
        if not isinstance(prov, dict) or prov.get('conflict') is not True:
            continue
        if prov.get('adjudications'):
            continue
        out.append((slug, identity, prov))
    return out


def _gtin_distinct(prov):
    """The disagreeing GTIN set, layer-appropriately.

    L2 receipts carry the gate's own `recovery.distinct_gtins`; L1 receipts
    derive it the same way the extractor's conflict flag did — distinct valid
    normalized codes across observations.
    """
    recovery = prov.get('recovery')
    if isinstance(recovery, dict) and recovery.get('distinct_gtins'):
        return [g for g in recovery['distinct_gtins'] if g]
    return sorted({o.get('gtin14') for o in prov.get('observations', ())
                   if isinstance(o, dict) and o.get('valid') and o.get('gtin14')})


def _gtin_l1_evidence_html(prov):
    """L1 evidence: every observation, in discovery order — source, raw,
    normalized GTIN-14, check-digit validity. The extractor's promise was
    'auditable without re-running'; this is where that promise is kept."""
    rows = ''.join(
        f'<tr><td>{_esc(o.get("source", ""))}</td>'
        f'<td class="fslug">{_esc(o.get("raw", ""))}</td>'
        f'<td class="fslug">{_esc(o.get("gtin14", "") or "")}</td>'
        f'<td>{"valid" if o.get("valid") else "invalid"}</td></tr>'
        for o in prov.get('observations', ()) if isinstance(o, dict))
    return (
        '<table class="ftable"><thead><tr><th>source</th><th>raw</th>'
        '<th>gtin-14</th><th>check</th></tr></thead>'
        f'<tbody>{rows}</tbody></table>')


def _gtin_l2_evidence_html(prov):
    """L2 evidence: the admission-gate receipt — query, verdict, timestamp,
    then every candidate with its clause-relevant facts (gtin, chosen_source,
    token_match, truncated title, resolve error if any)."""
    recovery = prov.get('recovery', {})
    if not isinstance(recovery, dict):
        return ''
    head = (
        '<div class="ids">'
        f'<span>query <code>{_esc(recovery.get("query", ""))}</code></span>'
        f'<span>verdict <code>{_esc(recovery.get("verdict", ""))}</code></span>'
        f'<span>model token <code>{_esc(recovery.get("model_token", ""))}</code></span>'
        f'<span>{_esc(recovery.get("recovered_at", ""))}</span>'
        '</div>')
    rows = ''.join(
        f'<tr><td class="fslug">{_esc(c.get("item_id", ""))}</td>'
        f'<td class="fslug">{_esc(c.get("epid", ""))}</td>'
        f'<td class="fslug">{_esc(c.get("gtin") or "")}</td>'
        f'<td>{_esc(c.get("chosen_source") or "")}</td>'
        f'<td>{"yes" if c.get("token_match") else "no"}</td>'
        f'<td class="ferr">{_esc(c.get("error") or c.get("title", ""))}</td></tr>'
        for c in recovery.get('candidates', ()) if isinstance(c, dict))
    table = (
        '<table class="ftable"><thead><tr><th>item</th><th>epid</th>'
        '<th>gtin</th><th>source</th><th>token</th><th>title / error</th>'
        '</tr></thead>'
        f'<tbody>{rows}</tbody></table>')
    return head + table


def _gtin_adjudication_html(slug, distinct):
    """The adjudication controls: one assign form per evidenced GTIN (the
    choice set IS the evidence — no free-text into an identity anchor; the
    route re-validates server-side, this is presentation not enforcement),
    plus a dismiss form with the structured reason vocabulary."""
    assign_forms = ''.join(
        f'<form method="post" action="/admin/gtin-resolve">'
        f'<input type="hidden" name="slug" value="{_esc(slug)}">'
        f'<input type="hidden" name="action" value="assign">'
        f'<input type="hidden" name="gtin" value="{_esc(g)}">'
        f'<button class="go" type="submit">assign '
        f'<code class="gtinchip">{_esc(g)}</code></button></form>'
        for g in distinct)
    reason_opts = ''.join(
        f'<option value="{_esc(r)}">{_esc(r)}</option>'
        for r in (skus_registry.DISMISS_REASONS if skus_registry else ()))
    dismiss_form = (
        '<form method="post" action="/admin/gtin-resolve">'
        f'<input type="hidden" name="slug" value="{_esc(slug)}">'
        '<input type="hidden" name="action" value="dismiss">'
        f'<label>reason<select name="reason">{reason_opts}</select></label>'
        '<button class="no" type="submit">dismiss — fallback identity</button>'
        '</form>')
    return (f'<div class="adjudication">{assign_forms}{dismiss_form}</div>')


def _gtin_conflict_card_html(slug, identity, prov):
    """One conflict receipt as an evidence card with adjudication controls.
    Layer badge from the receipt's own shape (a `recovery` block only exists
    on second-pass receipts), the disagreeing GTIN set up front, full evidence
    table below, human ruling forms last — read everything, then decide."""
    is_l2 = isinstance(prov.get('recovery'), dict)
    layer = 'L2 second-pass' if is_l2 else 'L1 own-listing'
    distinct = _gtin_distinct(prov)
    gtin_chips = ''.join(
        f'<code class="gtinchip">{_esc(g)}</code>' for g in distinct)
    vendor = identity.get('vendor', '')
    model = identity.get('model', '')
    evidence = (_gtin_l2_evidence_html(prov) if is_l2
                else _gtin_l1_evidence_html(prov))
    return (
        '<div class="card conflictcard">'
        f'<div class="meta"><h2>{_esc(slug)}</h2>'
        f'<div class="sub">{_esc(vendor)} {_esc(model)}</div></div>'
        '<div class="ids" style="margin-top:8px">'
        f'<span class="badge collision">{_esc(layer)} conflict</span>'
        f'<span>{len(distinct)} distinct GTINs</span>{gtin_chips}'
        '</div>'
        f'{evidence}'
        f'{_gtin_adjudication_html(slug, distinct)}'
        '</div>')


def _gtin_conflict_section_html():
    """The GTIN Conflicts section: unresolved Axis A conflict receipts, each
    with its full evidence trail and the human ruling controls (assign one of
    the evidenced GTINs, or dismiss to fallback identity with a structured
    reason). Resolution is APPEND-ONLY — the receipt is never mutated, the
    ruling is an event (skus_registry.adjudicate_gtin). Renders nothing when
    there are no receipts — conflicts are exceptional, not daily flow
    (failed-panel convention, not review-queue convention).
    """
    conflicts = _gtin_conflicts()
    if not conflicts:
        return ''
    cards = ''.join(
        _gtin_conflict_card_html(slug, identity, prov)
        for slug, identity, prov in conflicts)
    header = _GTIN_SECTION_HEADER.format(count=len(conflicts))
    return header + cards


def _render_page(banner_html=''):
    """Render the full /admin page: Review Ready (publish gate) leads, the slug
    Review Queue (identity adjudication) follows, the failed panel sits inside
    Review Ready as a collapsed health signal. A banner (from a POST outcome)
    renders above both sections."""
    ready_section = _review_ready_section_html()

    pending = review_queue.load_pending()
    if pending:
        slug_cards = ''.join(_card_html(r) for r in pending)
    else:
        slug_cards = ('<div class="empty">Queue is empty — '
                      'nothing awaiting identity review.</div>')
    slug_section = (
        _SLUG_SECTION_HEADER.format(count=len(pending)) + slug_cards)

    gtin_section = _gtin_conflict_section_html()

    body = ('<h1>AskMaddi Admin</h1>' + banner_html + ready_section
            + slug_section + gtin_section)
    page = _PAGE.format(body=body)
    return Response(page, mimetype='text/html')


def _banner(kind, msg):
    return f'<div class="banner {kind}">{_esc(msg)}</div>'


# --- Route registration -------------------------------------------------

def register_admin(app, render_runner=None):
    """Attach the admin review surface to a Flask app. Called by
    app_production under the HAS_CAPTURE guard.

    `render_runner` is the publish-time card renderer, callable(card_path) ->
    (rc, detail) (rc 0 == card is live). Defaults to the real build_site shell-
    out; tests inject a fake so the publish gate's auth/state/provenance logic is
    exercised with no filesystem render. Same injection discipline as the
    factory's build runner — the route stays pure, the subprocess is swappable.
    """
    if render_runner is None:
        render_runner = build_site_runner()

    def _gate():
        """Shared auth preamble. Returns a Response to short-circuit (503 if the
        surface isn't configured, 401 Basic challenge if unauthenticated), or
        None when the request is cleared to proceed."""
        if not _admin_token():
            return Response('admin surface not configured', status=503)
        if not _authed():
            return _challenge()
        return None

    @app.route('/admin', methods=['GET'])
    def admin_index():
        blocked = _gate()
        if blocked is not None:
            return blocked
        return _render_page()

    @app.route('/admin/promote', methods=['POST'])
    def admin_promote():
        blocked = _gate()
        if blocked is not None:
            return blocked
        qid = request.form.get('queue_id', '').strip()
        override_slug = request.form.get('override_slug', '').strip()
        if not qid or not override_slug:
            return _render_page(_banner('err', 'queue_id and override_slug required'))
        try:
            record, status = review_queue.promote(qid, override_slug)
        except review_queue.PromotionRejected as e:
            # The authorized slug STILL collides — visible, not a 500. Lee fixes
            # the slug and retries. Promotion is not a bypass.
            return _render_page(_banner('err', f'Promotion rejected: {e}'))
        except (KeyError, ValueError) as e:
            return _render_page(_banner('err', str(e)))
        slug = record.get('promoted_as', override_slug)
        return _render_page(_banner('ok', f'Promoted {slug} → spine ({status}).'))

    @app.route('/admin/reject', methods=['POST'])
    def admin_reject():
        blocked = _gate()
        if blocked is not None:
            return blocked
        qid = request.form.get('queue_id', '').strip()
        reason = request.form.get('reason', '').strip()
        if not qid:
            return _render_page(_banner('err', 'queue_id required'))
        try:
            review_queue.reject(qid, reason)
        except (KeyError, ValueError) as e:
            return _render_page(_banner('err', str(e)))
        return _render_page(_banner('ok', f'Rejected ({reason}) — routed upstream.'))

    @app.route('/admin/reresolve', methods=['POST'])
    def admin_reresolve():
        blocked = _gate()
        if blocked is not None:
            return blocked
        qid = request.form.get('queue_id', '').strip()
        item_id = request.form.get('item_id', '').strip()
        if not qid or not item_id:
            return _render_page(_banner('err', 'queue_id and item_id required'))
        if not _HAS_EBAY or not ebay_api.is_configured():
            return _render_page(_banner(
                'err', 're-resolve needs the eBay API (creds/module unavailable).'))
        try:
            review_queue.reresolve(qid, item_id, ebay=ebay_api)
        except ebay_api.EbayAPIError as e:
            return _render_page(_banner('err', f'eBay re-resolve failed: {e}'))
        except (KeyError, ValueError) as e:
            # Not pending / wrong reason / item_id not among candidates — all
            # visible, never a 500. The constraint is the message.
            return _render_page(_banner('err', str(e)))
        return _render_page(_banner(
            'ok', 'Re-resolved to chosen listing — review the refreshed identity, '
                  'then promote through the slug gate.'))

    # --- Piece 4: the card-factory publish gate ------------------------

    @app.route('/admin/publish', methods=['POST'])
    def admin_publish():
        """Publish a review_ready card LIVE: render it via build_site, then
        advance work_queue review_ready -> promoted. The render is the human's
        one in-the-loop touch; mark_published only runs AFTER a clean render, so
        a render failure leaves the record review_ready (retry-able), never a
        promoted state with no live card behind it. Re-asserts the integrity
        gate server-side (a readable card + a spine entry) so a stale/forged
        form can't publish a card the page disabled."""
        blocked = _gate()
        if blocked is not None:
            return blocked
        if not _HAS_WORK_QUEUE:
            return _render_page(_banner('err', 'work_queue unavailable'))
        slug = request.form.get('slug', '').strip()
        if not slug:
            return _render_page(_banner('err', 'slug required'))
        record = work_queue.get(slug)
        if record is None:
            return _render_page(_banner('err', f'no work-queue record {slug!r}'))
        if record.get('state') != 'review_ready':
            return _render_page(_banner(
                'err', f'{slug} is {record.get("state")!r}, not review_ready'))
        # Server-side re-assertion of the option-2 integrity gate.
        card_path = record.get('card_path')
        if _load_card(card_path) is None:
            return _render_page(_banner(
                'err', f'{slug}: assembled card.json missing/unreadable — '
                       'cannot publish.'))
        if _spine_entry(slug) is None:
            return _render_page(_banner(
                'err', f'{slug}: no spine entry — identity unavailable, '
                       'cannot publish.'))
        # Render live FIRST; only advance state on a clean render.
        rc, detail = render_runner(card_path)
        if rc != 0:
            return _render_page(_banner(
                'err', f'Render failed for {slug}: {detail} — left review_ready, '
                       'retry after fixing.'))
        try:
            work_queue.mark_published(slug)
        except (KeyError, ValueError) as e:
            return _render_page(_banner(
                'err', f'Rendered live but state advance failed: {e}'))
        return _render_page(_banner(
            'ok', f'Published {slug} — card is live.'))

    @app.route('/admin/reject-card', methods=['POST'])
    def admin_reject_card():
        """Reject a CLEAN review_ready card the human declines to publish — a
        content-quality signal routed upstream (work_queue.reject_card with a
        CARD_REJECT_REASONS code). Distinct from a build CRASH (failed) and from
        a slug reject (review_queue). Publishes nothing; parks the record in
        `rejected` as the adjudication log."""
        blocked = _gate()
        if blocked is not None:
            return blocked
        if not _HAS_WORK_QUEUE:
            return _render_page(_banner('err', 'work_queue unavailable'))
        slug = request.form.get('slug', '').strip()
        reason = request.form.get('reason', '').strip()
        if not slug:
            return _render_page(_banner('err', 'slug required'))
        try:
            work_queue.reject_card(slug, reason)
        except (KeyError, ValueError) as e:
            # Unknown reason / not review_ready — visible banner, never a 500.
            return _render_page(_banner('err', str(e)))
        return _render_page(_banner(
            'ok', f'Rejected {slug} ({reason}) — routed upstream, not published.'))

    # Card-identity fields the gate may override (images-on-spine step 6).
    # Deliberately a whitelist: the writer is generic, the ROUTE is not —
    # expanding the gate's reach is a one-tuple decision, not an open door.
    OVERRIDE_FIELDS = ('image_thumb',)

    @app.route('/admin/set-override', methods=['POST'])
    def admin_set_override():
        """Write one card-identity override to the SPINE (D3's writable
        layer). Thin form boundary over skus_registry.set_override, same
        doctrine as reject/gtin-resolve. Writes the spine only — the card
        bakes it on the NEXT assemble, so the success banner carries the
        re-assemble prompt. Publish stays behind the gate; this never
        renders anything live. Empty value CLEARS the override."""
        blocked = _gate()
        if blocked is not None:
            return blocked
        if skus_registry is None:
            return _render_page(_banner('err', 'skus_registry unavailable'))
        slug = request.form.get('slug', '').strip()
        field = request.form.get('field', '').strip()
        value = request.form.get('value', '').strip() or None
        if not slug:
            return _render_page(_banner('err', 'slug required'))
        if field not in OVERRIDE_FIELDS:
            return _render_page(_banner(
                'err', f'field {field!r} not overridable from the gate'))
        if field == 'image_thumb' and value is not None and \
                not value.startswith(('http://', 'https://')):
            return _render_page(_banner(
                'err', 'image override must be an http(s) URL'))
        status = skus_registry.set_override(slug, field, value)
        if status == 'written':
            return _render_page(_banner(
                'ok', f'Override written: {slug}.{field} — re-assemble '
                      f'{slug} to bake it into the card, then publish.'))
        if status == 'cleared':
            return _render_page(_banner(
                'ok', f'Override cleared: {slug}.{field} — next assemble '
                      f'falls back to the spine pick.'))
        if status == 'no-op':
            return _render_page(_banner('ok', f'{slug}.{field}: unchanged.'))
        return _render_page(_banner('err', f'{slug}: {status}'))

    @app.route('/admin/gtin-resolve', methods=['POST'])
    def admin_gtin_resolve():
        """Adjudicate a GTIN conflict receipt — the abstain->human contract's
        writing half. Delegates entirely to skus_registry.adjudicate_gtin
        (one gate definition; this route is a thin form boundary, same
        doctrine as promote/reject). APPEND-ONLY: the receipt survives the
        ruling intact, the ruling is an event. Every non-success status maps
        to a visible banner, never a 500 and never a partial write."""
        blocked = _gate()
        if blocked is not None:
            return blocked
        if skus_registry is None:
            return _render_page(_banner('err', 'skus_registry unavailable'))
        slug = request.form.get('slug', '').strip()
        action = request.form.get('action', '').strip()
        gtin = request.form.get('gtin', '').strip() or None
        reason = request.form.get('reason', '').strip() or None
        if not slug:
            return _render_page(_banner('err', 'slug required'))
        status = skus_registry.adjudicate_gtin(
            slug, action, gtin=gtin, reason=reason, actor='admin')
        if status == 'assigned':
            return _render_page(_banner(
                'ok', f'Assigned {gtin} to {slug} — receipt preserved, '
                      'ruling appended.'))
        if status == 'dismissed':
            return _render_page(_banner(
                'ok', f'Dismissed {slug} ({reason}) — falls to fallback '
                      'identity, ruling appended.'))
        return _render_page(_banner('err', f'{slug}: {status}'))

    return app
