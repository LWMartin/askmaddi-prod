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

import gtin_extract

EBAY_APP_ID = os.environ.get('EBAY_APP_ID', '')
EBAY_CERT_ID = os.environ.get('EBAY_CERT_ID', '')
EBAY_CAMPAIGN_ID = os.environ.get('EBAY_CAMPAIGN_ID', '')
EBAY_MARKETPLACE = os.environ.get('EBAY_MARKETPLACE', 'EBAY_US')

# Production endpoints (sandbox uses api.sandbox.ebay.com)
TOKEN_URL = 'https://api.ebay.com/identity/v1/oauth2/token'
BROWSE_SEARCH_URL = 'https://api.ebay.com/buy/browse/v1/item_summary/search'
# getItem — full per-item detail for lossless identity capture (skus registry).
# item_id is the RESTful form: v1|<legacyId>|<variationId>.
BROWSE_ITEM_URL = 'https://api.ebay.com/buy/browse/v1/item'
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


def _affiliate_headers(token, customid=None):
    """Standard Browse API headers, including EPN affiliate context.

    When EBAY_CAMPAIGN_ID is set, the X-EBAY-C-ENDUSERCTX header makes the
    returned itemAffiliateWebUrl trackable. An optional `customid` adds the
    EPN SubID (affiliateReferenceId, <=256 chars) so eBay bakes it into the
    affiliate URL — used for card-level revenue attribution. Per eBay docs the
    SubID is embedded in the customid part of the returned EPN link; we never
    string-append it ourselves (eBay constructs the tagged URL).
    """
    headers = {
        'Authorization': f'Bearer {token}',
        'X-EBAY-C-MARKETPLACE-ID': EBAY_MARKETPLACE,
        'Content-Type': 'application/json',
    }
    if EBAY_CAMPAIGN_ID:
        ctx = f'affiliateCampaignId={EBAY_CAMPAIGN_ID}'
        if customid:
            # 256-char cap per EPN spec; truncate defensively rather than 400.
            ref = str(customid)[:256]
            ctx += f',affiliateReferenceId={ref}'
        headers['X-EBAY-C-ENDUSERCTX'] = ctx
    return headers


def search(query, limit=10, customid=None):
    """Search eBay for `query`, return a list of normalized product dicts.

    Each dict: {name, price, currency, image, url, condition, seller}.
    URLs are affiliate-tagged when EBAY_CAMPAIGN_ID is set. An optional
    `customid` threads an EPN SubID through for card-level attribution; the
    thin result shape is unchanged so existing /ebay/search callers are
    unaffected (the param defaults to None).
    Raises EbayAPIError on failure.
    """
    if not query:
        return []

    token = _get_token()
    headers = _affiliate_headers(token, customid)

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


def search_candidates(query, limit=10):
    """Identity-resolution search: like search() but rows carry item_id.

    The public search() shape is frozen (the frontend depends on it — spec
    decision #4), so this is a SEPARATE path for the search->tap->resolve flow
    (back-fill, /ebay/resolve, GTIN second-pass). Rows include the RESTful
    item_id needed to call resolve(), plus epid/brand where the summary
    supplies them, so a scorer can rank candidates before paying the per-item
    resolve() cost.

    PROMOTED to public 2026-07-01 (substrate Amendment A build implication):
    the GTIN second-pass wire is a production caller and must not depend on an
    underscore-prefixed shape. `_search_candidates` remains as an alias so the
    existing call sites (resolve_sku, backfill_skus) and duck-typed test
    doubles are untouched; new code should use this name.

    Returns list of dicts: {item_id, title, price, currency, condition, epid, brand}.
    Raises EbayAPIError on failure.
    """
    if not query:
        return []

    token = _get_token()
    headers = _affiliate_headers(token)
    params = {'q': query, 'limit': str(min(int(limit), 50))}
    resp = requests.get(BROWSE_SEARCH_URL, headers=headers, params=params, timeout=15)
    if resp.status_code != 200:
        raise EbayAPIError(f'browse search failed: HTTP {resp.status_code}')

    items = resp.json().get('itemSummaries', []) or []
    out = []
    for it in items:
        price = it.get('price', {}) or {}
        out.append({
            'item_id': it.get('itemId', ''),
            'title': it.get('title', ''),
            'price': price.get('value', ''),
            'currency': price.get('currency', ''),
            'condition': it.get('condition', ''),
            'epid': it.get('epid', ''),
            'brand': it.get('brand', ''),
        })
    return out


# Back-compat alias (promotion 2026-07-01): existing callers (resolve_sku,
# backfill_skus) and duck-typed test doubles bind the underscore name. New
# code uses search_candidates.
_search_candidates = search_candidates


def _extract_identity(item):
    """Map an eBay getItem payload to the skus.json `identity` block (lossless).

    Pulls the registry-schema fields (epid, legacy_item_id, ebay_category_id,
    brand, mpn, market_title, image, price_seen) defensively — getItem returns
    some fields top-level and some inside the `product` container depending on
    fieldgroups, so each is read from both possible homes. brand/mpn that the
    live search() normalizer discards are the whole point of capture here.
    """
    product = item.get('product', {}) or {}
    price = item.get('price', {}) or {}
    image = (item.get('image', {}) or {}).get('imageUrl', '')
    # Catalog image (images-on-spine D4): product.imageUrls[0] is the eBay
    # CATALOG stock shot when present, vs item.image which is the seller's
    # listing gallery photo. Capture the catalog URL into its own field
    # whenever the container supplies it — additive, keeps the evidence so
    # the card mapper decides at consumption (catalog preferred, listing
    # fallback, human override on top). When item.image is absent the
    # existing fallback still fills `image` from the same catalog URL, so
    # image == image_catalog and downstream source-stamping stays exact.
    imgs = product.get('imageUrls', []) or []
    image_catalog = ''
    if imgs and isinstance(imgs[0], dict):
        image_catalog = imgs[0].get('imageUrl', '') or ''
    if not image:
        image = image_catalog

    # brand / mpn live under product.brand/product.mpn, or in localizedAspects.
    brand = product.get('brand', '') or item.get('brand', '')
    mpn = product.get('mpn', '') or item.get('mpn', '')
    if not brand or not mpn:
        for asp in (item.get('localizedAspects', []) or []):
            name = (asp.get('name') or '').lower()
            val = asp.get('value') or ''
            if not brand and name == 'brand':
                brand = val
            if not mpn and name in ('mpn', 'manufacturer part number'):
                mpn = val

    # GTIN extraction (substrate spec step 5). `item` here IS the raw payload
    # (resolve() stores it as _raw), so the code containers the probe read —
    # product.gtins / additionalProductIdentities / localizedAspects — are all
    # reachable directly. extract_gtin returns a canonical GTIN-14 + a full
    # provenance receipt (every code seen, source, validity, conflict flag).
    # Lands in identity.gtin / identity.gtin_provenance; the later substrate
    # migration hoists gtin to the top-level Axis A anchor. Additive, non-breaking.
    gtin_result = gtin_extract.extract_gtin(item)

    return {
        'epid': item.get('epid', '') or product.get('epid', ''),
        'legacy_item_id': item.get('legacyItemId', ''),
        'ebay_category_id': item.get('categoryId', ''),
        'brand': brand,
        'mpn': mpn,
        'gtin': gtin_result['gtin'],
        'gtin_provenance': gtin_result['gtin_provenance'],
        'market_title': item.get('title', ''),
        'image': image,
        'image_catalog': image_catalog,
        'price_seen': {
            'value': price.get('value', ''),
            'currency': price.get('currency', ''),
            'as_of': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        },
    }


def resolve(item_id, customid=None):
    """Re-fetch one eBay item's full detail for lossless identity capture.

    Sibling to search(): search() returns thin display rows for many items;
    resolve() returns the full canonical identity for ONE tapped item, the
    seed for a skus.json registry entry (demand-factory Stage 1). Only the
    chosen item pays the lossless-capture cost — display stays fast.

    Args:
      item_id:  RESTful eBay item id, form v1|<legacyId>|<variationId>.
      customid: optional EPN SubID for card-level affiliate attribution.

    Returns a dict:
      {'identity': {...registry schema...},
       'affiliate_url': <itemAffiliateWebUrl or itemWebUrl>,
       '_raw': <full getItem payload, nothing dropped at the seam>}

    Pure fetch — no filesystem side effects. The idempotent/atomic skus.json
    write is the registry-writer's job; this is the capture function it calls.
    Raises EbayAPIError on failure (incl. unknown item id).
    """
    if not item_id:
        raise EbayAPIError('resolve() requires an item_id')

    token = _get_token()
    headers = _affiliate_headers(token, customid)
    # PRODUCT fieldgroup surfaces brand/mpn/aspects — the alias source the
    # registry needs and the bare default response omits.
    params = {'fieldgroups': 'PRODUCT'}
    url = f"{BROWSE_ITEM_URL}/{item_id}"
    resp = requests.get(url, headers=headers, params=params, timeout=15)
    if resp.status_code != 200:
        raise EbayAPIError(f'getItem failed: HTTP {resp.status_code}')

    item = resp.json()
    affiliate_url = item.get('itemAffiliateWebUrl') or item.get('itemWebUrl', '')
    return {
        'identity': _extract_identity(item),
        'affiliate_url': affiliate_url,
        '_raw': item,
    }
