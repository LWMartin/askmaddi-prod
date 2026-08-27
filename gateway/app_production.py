"""
AskMaddi Gateway — Production
==============================
Merged from VPS (v74L, running since Feb 2026) and repo (v157L).

Provides:
- Site manifests (extraction instructions per retailer)
- CORS proxy (simple fetch OR headless for JS-rendered sites)
- Rate limiting (30/min per IP on proxy endpoint)
- Domain allowlist (exact match, not endswith)
- Anonymous analytics ping

Privacy: User queries stay in their browser.
         Source sites see our server, not the user.
         We do NOT log URLs, query strings, or user IPs.
"""

from flask import Flask, jsonify, request, Response
from flask_cors import CORS
import requests
import json
import os
import hashlib


def _load_dotenv():
    """Minimal .env loader — parses gateway/.env into os.environ if present.

    Thin wrapper preserved for call-site stability; the logic now lives in the
    shared env_bootstrap module (extracted 2026-06-30, item 3) so the cron entry
    points load the same secrets file the same way. No external dependency; only
    sets keys not already in the environment (systemd EnvironmentFile or shell
    exports still take precedence); silent no-op if the file is absent.
    """
    import env_bootstrap
    return env_bootstrap.load_dotenv()


_load_dotenv()

# --- Proxy Config ---
# Residential proxy (Webshare) routes scraper traffic through consumer IPs,
# defeating datacenter-IP bot detection at Amazon/eBay. Single PROXY_URL env
# var is shared by headless Chrome (--proxy-server) and the requests fallback.
PROXY_URL = os.environ.get('PROXY_URL', '')
PROXY_ENABLED = os.environ.get('PROXY_ENABLED', 'false').lower() == 'true'


def get_proxy_config():
    """Return proxy config dict, or None if disabled/unconfigured."""
    if not PROXY_ENABLED or not PROXY_URL:
        return None
    return {
        'url': PROXY_URL,
        'requests_proxies': {'http': PROXY_URL, 'https': PROXY_URL},
    }


# --- eBay Marketplace Account Deletion ---
# eBay requires every app using its APIs to host an endpoint that (a) answers a
# one-time GET challenge handshake and (b) acknowledges POST deletion events.
# The challenge response is a SHA-256 hash over the EXACT concatenation:
#   challengeCode + verificationToken + endpointURL
# (order matters — wrong order is the #1 cause of portal save failures).
# Both values come from env so they match the dev-portal entry character-for-char.
EBAY_VERIFICATION_TOKEN = os.environ.get('EBAY_VERIFICATION_TOKEN', '')
EBAY_DELETION_ENDPOINT = os.environ.get('EBAY_DELETION_ENDPOINT', '')


app = Flask(__name__)
CORS(app)

# --- Rate Limiting ---
# Try flask-limiter if available (installed on VPS), degrade gracefully
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    limiter = Limiter(
        app=app,
        key_func=get_remote_address,
        default_limits=[]
    )
    HAS_LIMITER = True
except ImportError:
    HAS_LIMITER = False

# --- Headless Fetcher ---
# Chrome required on host. Lazy-init on first headless request.
headless = None
HAS_HEADLESS = False
try:
    from headless_fetcher import HeadlessFetcher, get_site_config, needs_headless
    HAS_HEADLESS = True
except ImportError:
    pass

# --- eBay Browse API ---
HAS_EBAY_API = False
try:
    import ebay_api
    HAS_EBAY_API = True
except ImportError:
    pass

# --- Distribution measurement + vault (Phase 0, maddi-distribution v2.0) ---
# Unguarded imports BY DESIGN: both modules are stdlib-only files in this same
# directory (no third-party deps to be missing on the box), and the endpoints
# they back are load-bearing for Phase 0 — a gateway that silently dropped
# outbound/ai_referral events or subscriber emails would defeat the entire
# "measure before pushing" gate while looking healthy. Fail loud at import.
import analytics_log
import subscribers

# --- Demand factory (capture path) ---
# demand_log (upstream want-signal) + review_queue (slug-ambiguous subset) +
# slug_normalizer (the gate). Guarded together so a gateway missing any of them
# degrades to read-only resolve rather than 500-ing the whole service. The
# capture path is opt-in (capture=1); without these modules it is simply
# unavailable, never a hard error on the existing read-only contract.
HAS_CAPTURE = False
try:
    import demand_log
    import review_queue
    import slug_normalizer
    HAS_CAPTURE = True
except ImportError:
    pass

# Admin review surface (Phase 4) — the human end of the write-back loop. Depends
# on review_queue (the same import HAS_CAPTURE guards), so it registers only when
# capture is available. Fails closed on its own if ADMIN_TOKEN is unset.
if HAS_CAPTURE:
    try:
        import admin_surface
        admin_surface.register_admin(app)
    except ImportError:
        pass


def get_headless():
    """Get or create headless browser instance. Reinitializes if the driver is stale."""
    global headless
    if not HAS_HEADLESS:
        return None
    if headless is None:
        proxy = get_proxy_config()
        headless = HeadlessFetcher(proxy_url=proxy['url'] if proxy else None)
        headless.start()
    elif not headless.is_healthy():
        print("[gateway] Headless instance unhealthy — reinitializing")
        headless._reinitialize()
    return headless


# --- Manifests ---
MANIFESTS = {}
MANIFEST_DIR = os.path.join(os.path.dirname(__file__), 'manifests')

# Sites we currently have active affiliate relationships with.
# Manifests for other sites can stay on disk but won't be loaded
# (frontend won't see them, proxy will reject their domains).
# To enable a new site: add its name here and redeploy.
# 2026-05-26: eBay disabled — Akamai edge CDN blocks Hetzner IP range entirely.
# 2026-05-28: eBay served via the official Browse API (/ebay/search), NOT scraped.
#   It stays in ENABLED_SITES so its manifest loads and the frontend lists eBay
#   as a source — but the frontend routes eBay to the API branch, never /proxy.
#   The manifest's extraction config is now vestigial (kept for schema parity).
# 2026-07-27: amazon REMOVED. Associates was reinstated (askmaddi20-20), and it
#   is precisely BECAUSE we are an Associate again that we must stop scraping.
#   Two independent reasons, either one sufficient:
#     1. The agreement forbids displaying Amazon price / availability / star
#        ratings / review counts / imagery without Creators API credentials,
#        and this path existed to render exactly that (see manifests/amazon.json
#        extraction.fields). Untagged links do not cure it — the rule binds the
#        Associate's site, not just the tagged links on it.
#     2. It forbids automated access to Amazon at all, so the scrape itself is
#        exposure independent of what we choose to display.
#   Amazon is NOT gone from the product: it is now a tagged, price-free exit
#   (the #amazon-crosscheck link in index.html, and the per-card rung from
#   build_site.amazon_cta). We surface OUR product data and link out; we no
#   longer surface theirs. manifests/amazon.json is kept on disk — restoring
#   the rung is a one-line change here IF Creators API credentials ever land.
ENABLED_SITES = {'ebay'}


def load_manifests():
    """Load enabled site manifests from JSON files."""
    global MANIFESTS
    if not os.path.exists(MANIFEST_DIR):
        print(f"Warning: {MANIFEST_DIR} does not exist")
        return
    skipped = []
    for filename in os.listdir(MANIFEST_DIR):
        if filename.endswith('.json'):
            site_name = filename.replace('.json', '')
            if site_name not in ENABLED_SITES:
                skipped.append(site_name)
                continue
            with open(os.path.join(MANIFEST_DIR, filename), 'r') as f:
                MANIFESTS[site_name] = json.load(f)
    print(f"Loaded {len(MANIFESTS)} site manifests: {list(MANIFESTS.keys())}")
    if skipped:
        print(f"Skipped (not in ENABLED_SITES): {skipped}")


# --- Domain Allowlist ---
# Exact domain match — no endswith tricks.
def get_allowed_domains():
    """Build allowlist from loaded manifests."""
    domains = set()
    for m in MANIFESTS.values():
        d = m.get('domain', '')
        if d:
            domains.add(d.replace('www.', ''))
    return domains


def validate_domain(url):
    """Check URL domain against allowlist. Returns (ok, domain)."""
    try:
        parts = url.split('//')
        if len(parts) < 2:
            return False, None
        domain = parts[1].split('/')[0].replace('www.', '')
        allowed = get_allowed_domains()
        # Exact match only — not endswith (fixes spoofable-domain vuln)
        return domain in allowed, domain
    except Exception:
        return False, None


# --- Routes ---

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'ok',
        'manifests_loaded': len(MANIFESTS),
        'headless_ready': headless is not None and headless.is_healthy(),
        'rate_limiting': HAS_LIMITER,
        'proxy_enabled': PROXY_ENABLED,
        'proxy_configured': bool(PROXY_URL),
        'ebay_api_configured': HAS_EBAY_API and ebay_api.is_configured(),
    })


@app.route('/instructions', methods=['GET'])
def get_instructions():
    return jsonify({'sites': MANIFESTS, 'version': '1.1.0'})


@app.route('/instructions/<site>', methods=['GET'])
def get_site_instructions(site):
    if site in MANIFESTS:
        return jsonify(MANIFESTS[site])
    return jsonify({'error': f'Unknown site: {site}'}), 404


def _proxy_handler():
    """
    CORS proxy — fetches HTML and returns it.
    Uses headless browser for JS-rendered sites when available.

    Privacy: We do NOT log the URL or any part of the query.
    Source sites see us, not the user.
    """
    data = request.get_json()
    url = data.get('url')

    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    # Validate domain
    ok, domain = validate_domain(url)
    if not ok:
        return jsonify({'error': f'Domain not allowed: {domain}'}), 403

    try:
        # Check if this site needs headless and we have it
        use_headless = False
        site_config = {}

        if HAS_HEADLESS:
            site_config = get_site_config(url)
            use_headless = site_config.get('needs_headless', False)

        if use_headless:
            fetcher = get_headless()
            if fetcher:
                html = fetcher.fetch(
                    url,
                    wait_for_selector=site_config.get('wait_for'),
                    wait_time=site_config.get('wait_time', 3)
                )
            else:
                # Headless unavailable — fall back to simple fetch
                use_headless = False

        if not use_headless:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
            }
            response = requests.get(
                url, headers=headers, timeout=15,
                proxies=(get_proxy_config() or {}).get('requests_proxies')
            )
            html = response.text

        return Response(html, status=200, content_type='text/html; charset=utf-8')

    except Exception as e:
        # Log error type only — NOT the URL (privacy)
        print(f"[ERROR] Proxy fetch failed: {type(e).__name__}")
        return jsonify({'error': str(e)}), 500


# Apply rate limiting if available
if HAS_LIMITER:
    proxy_fetch = app.route('/proxy', methods=['POST'])(
        limiter.limit("30 per minute")(_proxy_handler)
    )
else:
    proxy_fetch = app.route('/proxy', methods=['POST'])(_proxy_handler)


@app.route('/ebay/deletion', methods=['GET', 'POST'])
def ebay_deletion():
    """eBay Marketplace Account Deletion/Closure notification endpoint.

    GET  — one-time verification handshake. eBay sends ?challenge_code=XXX;
           we respond 200 with {"challengeResponse": sha256(code+token+url)}.
    POST — actual deletion event. AskMaddi stores product data, not eBay user
           PII, so there is nothing to purge; we log and acknowledge with 200.
    """
    if request.method == 'GET':
        challenge_code = request.args.get('challenge_code')
        if not challenge_code:
            return jsonify({'error': 'missing challenge_code'}), 400
        if not EBAY_VERIFICATION_TOKEN or not EBAY_DELETION_ENDPOINT:
            print("[ebay] ERROR: EBAY_VERIFICATION_TOKEN or EBAY_DELETION_ENDPOINT not set")
            return jsonify({'error': 'endpoint not configured'}), 500
        # Hash order is fixed by eBay: challengeCode + verificationToken + endpoint
        h = hashlib.sha256()
        h.update(challenge_code.encode('utf-8'))
        h.update(EBAY_VERIFICATION_TOKEN.encode('utf-8'))
        h.update(EBAY_DELETION_ENDPOINT.encode('utf-8'))
        return jsonify({'challengeResponse': h.hexdigest()}), 200

    # POST — deletion notification. Acknowledge fast; no eBay PII stored here.
    print("[ebay] account deletion notification received — no stored PII to purge")
    return jsonify({'status': 'acknowledged'}), 200


@app.route('/ebay/search', methods=['GET'])
def ebay_search():
    """Server-side eBay product search via the Browse API.

    Replaces the HTML-scrape path. Returns clean JSON the frontend renders
    directly — no proxy, no extraction, affiliate-tagged URLs.
    """
    if not HAS_EBAY_API or not ebay_api.is_configured():
        return jsonify({'error': 'eBay API not configured', 'items': []}), 503
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'error': 'missing q', 'items': []}), 400
    try:
        limit = int(request.args.get('limit', 10))
    except ValueError:
        limit = 10
    try:
        items = ebay_api.search(query, limit=limit)
        return jsonify({'items': items, 'count': len(items)}), 200
    except ebay_api.EbayAPIError as e:
        # Error type only, never the query (privacy) and never the secret
        print(f"[ebay] search error: {e}")
        return jsonify({'error': str(e), 'items': []}), 502


@app.route('/search', methods=['GET'])
def precise_search():
    """Lane A — precise product research over eBay (Used) + Adorama (New).

    The parallel route: fans out both sanctioned sources server-side and runs the
    spine-anchored Sieve (classify -> rerank -> dedup-by-identity -> compose;
    canonical products first, a capped compatible/third-party tail). Additive —
    /ebay/search is untouched, so the frontend switch is reversible. Privacy: the
    query is never logged (only error types + counts).
    """
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'error': 'missing q', 'results': [], 'sections': []}), 400
    try:
        limit = int(request.args.get('limit', 25))
    except ValueError:
        limit = 25
    try:
        import search_lane_a
        payload = search_lane_a.precise_search(query, limit=limit)
        return jsonify(payload), 200
    except Exception as e:
        print(f"[search] lane-a error: {type(e).__name__}: {e}")
        return jsonify({'error': 'search failed', 'results': [], 'sections': []}), 502


@app.route('/adorama/search', methods=['GET'])
def adorama_search():
    """Fast Adorama feed-index search — the New lane for the STREAMING client.

    Index lookup only, no Sieve (the precision runs cheap client-side as results
    stream in). Mirrors /ebay/search's {items,count} shape so the frontend renders
    both sources through one path. Privacy: query never logged.
    """
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'error': 'missing q', 'items': []}), 400
    try:
        limit = int(request.args.get('limit', 25))
    except ValueError:
        limit = 25
    try:
        import adorama_index
        if not adorama_index.is_configured():
            return jsonify({'items': [], 'count': 0}), 200
        items = adorama_index.search(query, limit=limit)
        return jsonify({'items': items, 'count': len(items)}), 200
    except Exception as e:
        print(f"[adorama] search error: {type(e).__name__}: {e}")
        return jsonify({'error': 'search failed', 'items': []}), 502


@app.route('/ebay/resolve', methods=['GET'])
def ebay_resolve():
    """Lossless identity capture for ONE tapped eBay item (demand-factory Stage 1).

    Sibling to /ebay/search: search returns thin display rows for many items;
    resolve re-fetches the full canonical identity for one item id — the seed
    for a skus.json registry entry. Guarded by the same HAS_EBAY_API /
    is_configured() gate as /ebay/search and proxied via the existing
    Apache /ebay → :5001 ProxyPass (no Apache change needed).

    Query params:
      item_id  (required) RESTful eBay item id (v1|<legacyId>|<variationId>).
      customid (optional) EPN SubID for card-level affiliate attribution.
      raw      (optional) raw=1 includes the full getItem payload (_raw).
               Omitted by default — the full payload is heavy and only the
               registry-writer needs it; the browser path stays lean.

    Capture mode (Phase 3 — opt-in via capture=1):
      When capture=1, this read-only route becomes the live demand WRITER.
      Two writes, in strict order, both OUTSIDE the skus.json spine:

        1. demand_log.log_unmet(category, identity)  — UNCONDITIONAL.
           Fires on every capture tap, the moment the want exists. This is the
           durable want-signal; it does not depend on the slug decision and is
           logged even if everything downstream is clean. category + ts +
           resolved identity only — never the raw query (privacy line).

        2. review_queue.enqueue(...)  — ONLY when the slug gate trips.
           slug_normalizer.resolve_slug(vendor, model) runs the SAME gate
           backfill uses. If the resolution is ambiguous (needs_review or a
           collision), the resolved identity is enqueued for async human
           adjudication. A clean resolution enqueues NOTHING — the tap is
           recorded as demand but needs no review.

      Capture NEVER writes skus.json. Promotion into the spine stays the
      human-authorized review_queue.promote() path; this route only captures.

      Capture requires vendor, model, category (the controlled-vocab human
      identity resolve() can't infer from a market title). Missing any → 400.
      The response gains a `capture` block reporting what was written:
        {'demand_logged': True,
         'queued': <queue_id or None>,
         'slug': <proposed/frozen slug>,
         'needs_review': <bool>}
    """
    if not HAS_EBAY_API or not ebay_api.is_configured():
        return jsonify({'error': 'eBay API not configured'}), 503
    item_id = request.args.get('item_id', '').strip()
    if not item_id:
        return jsonify({'error': 'missing item_id'}), 400
    customid = request.args.get('customid', '').strip() or None
    include_raw = request.args.get('raw', '') == '1'
    capture = request.args.get('capture', '') == '1'

    # Capture-mode preflight: validate the human-identity params BEFORE the
    # (billable, network) resolve() call, so a malformed capture request fails
    # fast without burning an eBay round-trip. Read-only resolve is unaffected.
    if capture:
        if not HAS_CAPTURE:
            return jsonify({'error': 'capture not available'}), 503
        vendor = request.args.get('vendor', '').strip()
        model = request.args.get('model', '').strip()
        category = request.args.get('category', '').strip()
        missing = [n for n, v in
                   (('vendor', vendor), ('model', model), ('category', category))
                   if not v]
        if missing:
            return jsonify({'error': f'capture requires {", ".join(missing)}'}), 400

    try:
        result = ebay_api.resolve(item_id, customid=customid)
        payload = {
            'identity': result['identity'],
            'affiliate_url': result['affiliate_url'],
        }
        if include_raw:
            payload['_raw'] = result['_raw']

        if capture:
            # (1) Unconditional want-signal, upstream of any slug decision.
            demand_log.log_unmet(category, identity=result['identity'])

            # (2) Slug gate — same function backfill/promote use. Enqueue ONLY
            #     the ambiguous subset; a clean resolution needs no review.
            res = slug_normalizer.resolve_slug(vendor, model)
            queued_id = None
            if res.needs_review or res.collision:
                record = review_queue.enqueue(
                    res, result, vendor, model, category)
                queued_id = record['queue_id']

            payload['capture'] = {
                'demand_logged': True,
                'queued': queued_id,
                'slug': res.slug,
                'needs_review': bool(res.needs_review or res.collision),
            }

        return jsonify(payload), 200
    except ebay_api.EbayAPIError as e:
        # Error type only, never the item_id (privacy) and never the secret
        print(f"[ebay] resolve error: {e}")
        # `upstream_status` lets a caller separate a DEAD LISTING (404) from a
        # transient failure (429/5xx) or a credential problem (401). The HTTP
        # status stays 502 for all of them: this route's contract is "the
        # upstream call failed", and changing it per-cause would break any
        # consumer keying on 502. Additive field, no contract change.
        return jsonify({'error': str(e),
                        'upstream_status': e.status_code}), 502


@app.route('/ping', methods=['POST'])
def analytics_ping():
    """Anonymous analytics — category only, never the query.

    Phase 0 (maddi-distribution v2.0, 2026-07-17): pings that carry a known
    'event' field ('outbound' | 'ai_referral') are PERSISTED via analytics_log
    (append-only JSONL, whitelisted values, no user data). Legacy pings — the
    original search-time category/source_count shape — keep their print-only
    behavior unchanged; nothing that previously reached this endpoint gains
    persistence retroactively.
    """
    data = request.get_json(silent=True) or {}
    event = data.get('event')
    if event in analytics_log.EVENT_TYPES:
        analytics_log.log_event(
            event,
            category=data.get('category'),
            retailer=data.get('retailer'),
            engine=data.get('engine'),
        )
        return jsonify({'received': True})

    category = data.get('category', 'unknown')
    source_count = data.get('source_count', 0)
    # Category-level only. No user tracking. Ever.
    print(f"[PING] category={category}, sources={source_count}")
    return jsonify({'received': True})


@app.route('/subscribe', methods=['POST'])
def subscribe():
    """Email capture — the vault (maddi-distribution v2.0 Phase 0).

    Honeypot: the form includes a visually hidden 'website' field. Humans leave
    it empty; bots fill it. A filled honeypot returns the SAME success response
    and writes nothing — never teach the bot which field tripped it.
    Response never distinguishes 'added' from 'exists' (no address-book oracle).
    """
    data = request.get_json(silent=True) or {}
    if data.get('website'):
        return jsonify({'ok': True})
    status = subscribers.add(data.get('email'), source='site')
    if status == 'invalid':
        return jsonify({'ok': False,
                        'error': 'Please enter a valid email address.'}), 400
    return jsonify({'ok': True})


def _unsub_page(message, ok=True):
    """Tiny self-contained confirmation page — matches the digest's calm voice,
    no JS, no external assets."""
    color = '#14532d' if ok else '#8a1c1c'
    return Response(
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>AskMaddi — Unsubscribe</title></head>'
        '<body style="margin:0 auto;max-width:520px;padding:48px 20px;'
        'font:16px/1.55 -apple-system,\'Segoe UI\',Georgia,serif;color:#1e2430;'
        'background:#faf9f6">'
        f'<h1 style="font-size:20px;color:{color}">{message}</h1>'
        '<p style="color:#6b7280"><a href="https://askmaddi.com/" '
        'style="color:#14532d">Return to AskMaddi</a></p></body></html>',
        mimetype='text/html')


@app.route('/unsubscribe', methods=['GET', 'POST'])
def unsubscribe():
    """One-click + link opt-out (maddi-digest §Email; CAN-SPAM).

    The digest email carries the address + a keyed token in both a clickable
    link (GET) and a List-Unsubscribe header (POST, RFC 8058 one-click). Both
    verify the HMAC token before suppressing, so nobody can unsubscribe anyone
    else by guessing an address. POST (mail-client one-click) returns 200 with
    no body; GET (human clicked) returns a small confirmation page.
    """
    email = request.args.get('e') or request.form.get('e')
    token = request.args.get('t') or request.form.get('t')
    norm = subscribers.normalize(email)
    if not norm or not subscribers.verify_unsubscribe_token(norm, token):
        if request.method == 'POST':
            return ('', 400)
        return _unsub_page('This unsubscribe link is invalid.', ok=False), 400
    subscribers.suppress(norm)
    if request.method == 'POST':
        return ('', 200)
    return _unsub_page("You're unsubscribed. You won't get the weekly digest.")


@app.teardown_appcontext
def cleanup(exception=None):
    """Cleanup on shutdown."""
    global headless
    if headless:
        headless.stop()


# --- Init ---
load_manifests()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"\n=== AskMaddi Gateway ===")
    print(f"Running on port {port}")
    print(f"Headless: {'ready' if HAS_HEADLESS else 'not available'}")
    print(f"Rate limiting: {'active' if HAS_LIMITER else 'not available'}")
    print("========================\n")
    app.run(host='0.0.0.0', port=port)
