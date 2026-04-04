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


def get_headless():
    """Get or create headless browser instance."""
    global headless
    if not HAS_HEADLESS:
        return None
    if headless is None:
        headless = HeadlessFetcher()
        headless.start()
    return headless


# --- Manifests ---
MANIFESTS = {}
MANIFEST_DIR = os.path.join(os.path.dirname(__file__), 'manifests')


def load_manifests():
    """Load all site manifests from JSON files."""
    global MANIFESTS
    if not os.path.exists(MANIFEST_DIR):
        print(f"Warning: {MANIFEST_DIR} does not exist")
        return
    for filename in os.listdir(MANIFEST_DIR):
        if filename.endswith('.json'):
            site_name = filename.replace('.json', '')
            with open(os.path.join(MANIFEST_DIR, filename), 'r') as f:
                MANIFESTS[site_name] = json.load(f)
    print(f"Loaded {len(MANIFESTS)} site manifests: {list(MANIFESTS.keys())}")


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
        'headless_ready': headless is not None and hasattr(headless, 'initialized') and headless.initialized,
        'rate_limiting': HAS_LIMITER
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
            response = requests.get(url, headers=headers, timeout=15)
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


@app.route('/ping', methods=['POST'])
def analytics_ping():
    """Anonymous analytics — category only, never the query."""
    data = request.get_json()
    category = data.get('category', 'unknown')
    source_count = data.get('source_count', 0)
    # Category-level only. No user tracking. Ever.
    print(f"[PING] category={category}, sources={source_count}")
    return jsonify({'received': True})


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
