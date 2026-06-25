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
import os
import html as _html

from flask import request, Response

import review_queue


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


def _reason_badge(record):
    """The decision driver: collision (clash with an existing spine slug) vs
    needs_review (ambiguous normalization, no direct clash)."""
    reason = record.get('reason', '')
    if reason == 'collision':
        return ('collision', record.get('collision_with') or '?')
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
</style></head>
<body>
  <h1>Review Queue</h1>
  <p class="lead">{count} pending · adjudicate into the skus.json spine or reject upstream</p>
  {banner}
  {body}
</body></html>"""


def _render_page(banner_html=''):
    pending = review_queue.load_pending()
    if pending:
        body = ''.join(_card_html(r) for r in pending)
    else:
        body = '<div class="empty">Queue is empty — nothing awaiting review.</div>'
    page = _PAGE.format(count=len(pending), banner=banner_html, body=body)
    return Response(page, mimetype='text/html')


def _banner(kind, msg):
    return f'<div class="banner {kind}">{_esc(msg)}</div>'


# --- Route registration -------------------------------------------------

def register_admin(app):
    """Attach the admin review surface to a Flask app. Called by
    app_production under the HAS_CAPTURE guard."""

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

    return app
