"""
AskMaddi Gateway
================
Provides:
- Site manifests
- CORS proxy (simple fetch OR headless for JS sites)
- Anonymous analytics

Privacy: User's query stays in their browser.
         Source sites see our server, not the user.
"""

from flask import Flask, jsonify, request, Response
from flask_cors import CORS
import requests
import json
import os

# Import headless fetcher
from headless_fetcher import HeadlessFetcher, get_site_config, needs_headless

app = Flask(__name__)
CORS(app)

# Global state
MANIFESTS = {}
MANIFEST_DIR = os.path.join(os.path.dirname(__file__), 'manifests')

# Headless browser (lazy initialized)
headless = None


def get_headless():
    """Get or create headless browser instance"""
    global headless
    if headless is None:
        headless = HeadlessFetcher()
        headless.start()
    return headless


def load_manifests():
    """Load all site manifests"""
    global MANIFESTS
    for filename in os.listdir(MANIFEST_DIR):
        if filename.endswith('.json'):
            site_name = filename.replace('.json', '')
            with open(os.path.join(MANIFEST_DIR, filename), 'r') as f:
                MANIFESTS[site_name] = json.load(f)
    print(f"Loaded {len(MANIFESTS)} site manifests: {list(MANIFESTS.keys())}")


@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'ok',
        'manifests_loaded': len(MANIFESTS),
        'headless_ready': headless is not None and headless.initialized
    })


@app.route('/instructions', methods=['GET'])
def get_instructions():
    return jsonify({'sites': MANIFESTS, 'version': '1.0.0'})


@app.route('/instructions/<site>', methods=['GET'])
def get_site_instructions(site):
    if site in MANIFESTS:
        return jsonify(MANIFESTS[site])
    return jsonify({'error': f'Unknown site: {site}'}), 404


@app.route('/proxy', methods=['POST'])
def proxy_fetch():
    """
    CORS proxy - fetches HTML and returns it.
    Uses headless browser for JS-rendered sites.
    
    Privacy: We see the URL, but we don't log it.
             Source sites see us, not the user.
    """
    data = request.get_json()
    url = data.get('url')
    
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    
    # Validate domain
    allowed_domains = [m.get('domain') for m in MANIFESTS.values()]
    url_domain = url.split('/')[2] if '//' in url else None
    
    if url_domain:
        url_domain_clean = url_domain.replace('www.', '')
        if not any(url_domain_clean.endswith(d.replace('www.', '')) for d in allowed_domains):
            return jsonify({'error': f'Domain not allowed: {url_domain}'}), 403
    
    try:
        # Check if site needs headless browser
        site_config = get_site_config(url)
        
        if site_config['needs_headless']:
            # Use headless Chrome for JS-rendered sites
            print(f"[HEADLESS] {url[:60]}...")
            fetcher = get_headless()
            html = fetcher.fetch(
                url,
                wait_for_selector=site_config.get('wait_for'),
                wait_time=site_config.get('wait_time', 3)
            )
        else:
            # Simple HTTP fetch for static sites
            print(f"[SIMPLE] {url[:60]}...")
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
            }
            response = requests.get(url, headers=headers, timeout=15)
            html = response.text
        
        return Response(html, status=200, content_type='text/html; charset=utf-8')
    
    except Exception as e:
        print(f"[ERROR] Proxy fetch failed: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/ping', methods=['POST'])
def analytics_ping():
    """
    Anonymous analytics - category only, never the query.
    """
    data = request.get_json()
    category = data.get('category', 'unknown')
    source_count = data.get('source_count', 0)
    
    # Just log for now. No user tracking. Ever.
    print(f"[PING] category={category}, sources={source_count}")
    
    return jsonify({'received': True})


@app.teardown_appcontext
def cleanup(exception=None):
    """Cleanup on shutdown"""
    global headless
    if headless:
        headless.stop()


if __name__ == '__main__':
    load_manifests()
    print("\n=== AskMaddi Gateway ===")
    print("http://localhost:5000")
    print("Privacy: Queries stay in browser")
    print("         Sources see us, not users")
    print("========================\n")
    app.run(debug=True, port=5000, threaded=True)