"""
Unit tests for ebay_api — resolve() lossless identity capture + customid SubID.
========================================================================
Offline only: no live eBay credentials, no network. The network-dependent
registry behaviors (idempotency, atomic write, contamination_key bridge) are
tested with the registry-writer, not here — resolve() itself is a pure fetch
and its mapping/header logic is what these tests pin.
"""
import ebay_api


# ─── _extract_identity: lossless field mapping ──────────────────────────────

def _full_payload():
    return {
        'itemId': 'v1|123456789012|0',
        'legacyItemId': '123456789012',
        'title': 'Sony Alpha A7 IV Mirrorless Camera Body ILCE-7M4',
        'categoryId': '31388',
        'epid': '15042899333',
        'price': {'value': '2498.00', 'currency': 'USD'},
        'image': {'imageUrl': 'https://i.ebayimg.com/x.jpg'},
        'product': {'brand': 'Sony', 'mpn': 'ILCE-7M4'},
        'itemAffiliateWebUrl': 'https://www.ebay.com/itm/123?campid=5339138080&customid=sony-a7iv',
        'itemWebUrl': 'https://www.ebay.com/itm/123',
        'localizedAspects': [{'type': 'STRING', 'name': 'Brand', 'value': 'Sony'}],
    }


def test_extract_identity_maps_all_schema_fields():
    idn = ebay_api._extract_identity(_full_payload())
    assert idn['epid'] == '15042899333'
    assert idn['legacy_item_id'] == '123456789012'
    assert idn['ebay_category_id'] == '31388'
    assert idn['brand'] == 'Sony'
    assert idn['mpn'] == 'ILCE-7M4'
    assert idn['market_title'].startswith('Sony Alpha A7 IV')
    assert idn['image'] == 'https://i.ebayimg.com/x.jpg'
    assert idn['price_seen']['value'] == '2498.00'
    assert idn['price_seen']['currency'] == 'USD'
    assert idn['price_seen']['as_of'].endswith('Z')


def test_extract_identity_brand_mpn_from_aspects_when_product_empty():
    # No product container — brand/mpn must fall through to localizedAspects.
    p = _full_payload()
    p['product'] = {}
    p['localizedAspects'] = [
        {'name': 'Brand', 'value': 'Sony'},
        {'name': 'MPN', 'value': 'ILCE-7M4'},
    ]
    idn = ebay_api._extract_identity(p)
    assert idn['brand'] == 'Sony'
    assert idn['mpn'] == 'ILCE-7M4'


def test_extract_identity_tolerates_missing_fields():
    # Sparse payload (e.g. a listing with no epid/category) must not throw.
    idn = ebay_api._extract_identity({'title': 'Some Item'})
    assert idn['market_title'] == 'Some Item'
    assert idn['epid'] == ''
    assert idn['brand'] == ''
    assert idn['price_seen']['value'] == ''


def test_extract_identity_image_from_product_imageurls_fallback():
    p = _full_payload()
    p['image'] = {}
    p['product']['imageUrls'] = [{'imageUrl': 'https://i.ebayimg.com/prod.jpg'}]
    idn = ebay_api._extract_identity(p)
    assert idn['image'] == 'https://i.ebayimg.com/prod.jpg'
    # Fallback case: image WAS the catalog shot, so both fields agree —
    # downstream source-stamping ('ebay_catalog') derives from the equality.
    assert idn['image_catalog'] == 'https://i.ebayimg.com/prod.jpg'


# ─── image_catalog capture (images-on-spine D4) ─────────────────────────────

def test_extract_identity_captures_both_listing_and_catalog_images():
    # Both containers present: image keeps the listing photo (precedence
    # unchanged), image_catalog carries the stock shot alongside it.
    p = _full_payload()
    p['product']['imageUrls'] = [{'imageUrl': 'https://i.ebayimg.com/catalog.jpg'}]
    idn = ebay_api._extract_identity(p)
    assert idn['image'] == 'https://i.ebayimg.com/x.jpg'
    assert idn['image_catalog'] == 'https://i.ebayimg.com/catalog.jpg'


def test_extract_identity_image_catalog_empty_when_no_product_images():
    # No product.imageUrls (the _full_payload default): listing photo fills
    # image, image_catalog stays '' — absence is honest, mapper falls back.
    idn = ebay_api._extract_identity(_full_payload())
    assert idn['image'] == 'https://i.ebayimg.com/x.jpg'
    assert idn['image_catalog'] == ''


def test_extract_identity_image_catalog_tolerates_malformed_list():
    # Defensive shape handling mirrors the existing image fallback: a bare
    # string entry (non-dict) must not throw and must not populate the field.
    p = _full_payload()
    p['product']['imageUrls'] = ['https://i.ebayimg.com/bare-string.jpg']
    idn = ebay_api._extract_identity(p)
    assert idn['image'] == 'https://i.ebayimg.com/x.jpg'
    assert idn['image_catalog'] == ''


# ─── _affiliate_headers: the EPN SubID contract ─────────────────────────────

def test_affiliate_headers_campaign_only(monkeypatch):
    monkeypatch.setattr(ebay_api, 'EBAY_CAMPAIGN_ID', '5339138080')
    h = ebay_api._affiliate_headers('tok')
    ctx = h['X-EBAY-C-ENDUSERCTX']
    assert 'affiliateCampaignId=5339138080' in ctx
    assert 'affiliateReferenceId' not in ctx


def test_affiliate_headers_with_customid_adds_subid(monkeypatch):
    monkeypatch.setattr(ebay_api, 'EBAY_CAMPAIGN_ID', '5339138080')
    h = ebay_api._affiliate_headers('tok', customid='sony-a7iv')
    ctx = h['X-EBAY-C-ENDUSERCTX']
    assert 'affiliateCampaignId=5339138080' in ctx
    assert 'affiliateReferenceId=sony-a7iv' in ctx


def test_affiliate_headers_customid_truncated_to_256(monkeypatch):
    monkeypatch.setattr(ebay_api, 'EBAY_CAMPAIGN_ID', '5339138080')
    long_id = 'x' * 400
    h = ebay_api._affiliate_headers('tok', customid=long_id)
    ref = h['X-EBAY-C-ENDUSERCTX'].split('affiliateReferenceId=')[1]
    assert len(ref) == 256


def test_affiliate_headers_no_campaign_no_ctx(monkeypatch):
    # No campaign id configured -> no affiliate context header at all.
    monkeypatch.setattr(ebay_api, 'EBAY_CAMPAIGN_ID', '')
    h = ebay_api._affiliate_headers('tok', customid='sony-a7iv')
    assert 'X-EBAY-C-ENDUSERCTX' not in h
    assert h['Authorization'] == 'Bearer tok'


def test_affiliate_headers_carries_bearer_and_marketplace(monkeypatch):
    monkeypatch.setattr(ebay_api, 'EBAY_MARKETPLACE', 'EBAY_US')
    h = ebay_api._affiliate_headers('mytoken')
    assert h['Authorization'] == 'Bearer mytoken'
    assert h['X-EBAY-C-MARKETPLACE-ID'] == 'EBAY_US'


# ─── resolve(): guard + mapping (network mocked) ────────────────────────────

def test_resolve_empty_item_id_raises():
    import pytest
    with pytest.raises(ebay_api.EbayAPIError):
        ebay_api.resolve('')


def test_resolve_maps_payload_and_prefers_affiliate_url(monkeypatch):
    payload = _full_payload()

    class _Resp:
        status_code = 200
        def json(self):
            return payload

    monkeypatch.setattr(ebay_api, '_get_token', lambda: 'tok')
    monkeypatch.setattr(ebay_api, 'EBAY_CAMPAIGN_ID', '5339138080')
    captured = {}

    def _fake_get(url, headers=None, params=None, timeout=None):
        captured['url'] = url
        captured['params'] = params
        captured['headers'] = headers
        return _Resp()

    monkeypatch.setattr(ebay_api.requests, 'get', _fake_get)

    out = ebay_api.resolve('v1|123456789012|0', customid='sony-a7iv')
    # getItem endpoint + PRODUCT fieldgroup
    assert captured['url'].endswith('/buy/browse/v1/item/v1|123456789012|0')
    assert captured['params'] == {'fieldgroups': 'PRODUCT'}
    # SubID threaded into the affiliate header
    assert 'affiliateReferenceId=sony-a7iv' in captured['headers']['X-EBAY-C-ENDUSERCTX']
    # lossless identity + affiliate url preferred + raw carried
    assert out['identity']['epid'] == '15042899333'
    assert out['affiliate_url'].startswith('https://www.ebay.com/itm/123?campid=')
    assert out['_raw'] is payload


def test_resolve_raises_on_http_error(monkeypatch):
    import pytest

    class _Resp:
        status_code = 404
        def json(self):
            return {}

    monkeypatch.setattr(ebay_api, '_get_token', lambda: 'tok')
    monkeypatch.setattr(ebay_api.requests, 'get',
                        lambda *a, **k: _Resp())
    with pytest.raises(ebay_api.EbayAPIError):
        ebay_api.resolve('v1|999|0')
