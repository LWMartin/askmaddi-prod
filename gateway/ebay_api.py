"""
eBay Browse API Client
======================
Server-side eBay product search via the official Browse API, replacing the
HTML-scrape path (which Akamai blocks on datacenter IPs).

Auth: OAuth2 client-credentials grant.
  base64(AppID:CertID) -> POST token endpoint -> bearer token (~2h TTL).
Token is cached in-process and refreshed on expiry.

Affiliate attribution: pass EPN Campaign ID via the X-EBAY-C-ENDUSERCTX
header so item URLs returned are affiliate-tagged and clicks attribute to us.

Credentials come from env (never hardcoded):
  EBAY_APP_ID         — App ID / Client ID
  EBAY_CERT_ID        — Cert ID / Client Secret
  EBAY_CAMPAIGN_ID    — EPN Campaign ID (optional; enables affiliate links)
  EBAY_MARKETPLACE    — default EBAY_US
"""

import os
import time
import base64
import requests

EBAY_APP_ID = os.environ.get('EBAY_APP_ID', '')
EBAY_CERT_ID = os.environ.get('EBAY_CERT_ID', '')
EBAY_CAMPAIGN_ID = os.environ.get('EBAY_CAMPAIGN_ID', '')
EBAY_MARKETPLACE = os.environ.get('EBAY_MARKETPLACE', 'EBAY_US')

# Production endpoints (sandbox uses api.sandbox.ebay.com)
TOKEN_URL = 'https://api.ebay.com/identity/v1/oauth2/token'
BROWSE_SEARCH_URL = 'https://api.ebay.com/buy/browse/v1/item_summary/search'
# Browse API public-data scope
SCOPE = 'https://api.ebay.com/oauth/api_scope'


class EbayAPIError(Exception):
    """Raised when the eBay API call fails or credentials are missing."""


# Module-level token cache: {'token': str, 'expires_at': float}
_token_cache = {'token': None, 'expires_at': 0.0}


def is_configured():
    """True if the minimum credentials for an API call are present."""
    return bool(EBAY_APP_ID and EBAY_CERT_ID)


def _get_token():
    """Return a valid bearer token, fetching/refreshing if needed.

    Cached in-process with a 60s safety margin before the stated expiry.
    """
    now = time.time()
    if _token_cache['token'] and now < _token_cache['expires_at'] - 60:
        return _token_cache['token']

    if not is_configured():
        raise EbayAPIError('EBAY_APP_ID / EBAY_CERT_ID not set')

    creds = f'{EBAY_APP_ID}:{EBAY_CERT_ID}'.encode('utf-8')
    auth_header = base64.b64encode(creds).decode('utf-8')
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Authorization': f'Basic {auth_header}',
    }
    data = {'grant_type': 'client_credentials', 'scope': SCOPE}

    resp = requests.post(TOKEN_URL, headers=headers, data=data, timeout=15)
    if resp.status_code != 200:
        # Don't leak the secret; report status + short reason only
        raise EbayAPIError(f'token request failed: HTTP {resp.status_code}')
    payload = resp.json()
    token = payload.get('access_token')
    expires_in = payload.get('expires_in', 7200)
    if not token:
        raise EbayAPIError('token response missing access_token')

    _token_cache['token'] = token
    _token_cache['expires_at'] = now + float(expires_in)
    return token


def search(query, limit=10):
    """Search eBay for `query`, return a list of normalized product dicts.

    Each dict: {name, price, currency, image, url, condition, seller}.
    URLs are affiliate-tagged when EBAY_CAMPAIGN_ID is set.
    Raises EbayAPIError on failure.
    """
    if not query:
        return []

    token = _get_token()
    headers = {
        'Authorization': f'Bearer {token}',
        'X-EBAY-C-MARKETPLACE-ID': EBAY_MARKETPLACE,
        'Content-Type': 'application/json',
    }
    # Affiliate context — makes returned itemAffiliateWebUrl trackable.
    if EBAY_CAMPAIGN_ID:
        headers['X-EBAY-C-ENDUSERCTX'] = (
            f'affiliateCampaignId={EBAY_CAMPAIGN_ID}'
        )

    params = {'q': query, 'limit': str(min(int(limit), 50))}
    resp = requests.get(BROWSE_SEARCH_URL, headers=headers, params=params, timeout=15)
    if resp.status_code != 200:
        raise EbayAPIError(f'browse search failed: HTTP {resp.status_code}')

    items = resp.json().get('itemSummaries', []) or []
    results = []
    for it in items:
        price = it.get('price', {}) or {}
        image = (it.get('image', {}) or {}).get('imageUrl', '')
        seller = (it.get('seller', {}) or {}).get('username', '')
        # Prefer the affiliate-tagged URL when present
        url = it.get('itemAffiliateWebUrl') or it.get('itemWebUrl', '')
        results.append({
            'name': it.get('title', ''),
            'price': price.get('value', ''),
            'currency': price.get('currency', ''),
            'image': image,
            'url': url,
            'condition': it.get('condition', ''),
            'seller': seller,
        })
    return results
