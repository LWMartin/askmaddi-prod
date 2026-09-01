"""
Unit tests for skus_registry — idempotent atomic skus.json writer.
==================================================================
Pins the maddi-skus-registry decision #3 properties: idempotency (re-resolve
is a no-op), atomic write, last-write-wins on genuine change. All offline,
tmp_path-isolated — no real data/skus.json touched.
"""
import json
import os

import skus_registry as reg


def _resolved(epid='15042899333', legacy='123456789012', mpn='ILCE-7M4',
              price='2498.00', title='Sony Alpha A7 IV'):
    return {
        'identity': {
            'epid': epid,
            'legacy_item_id': legacy,
            'ebay_category_id': '31388',
            'brand': 'Sony',
            'mpn': mpn,
            'market_title': title,
            'image': 'https://i.ebayimg.com/x.jpg',
            'price_seen': {'value': price, 'currency': 'USD', 'as_of': '2026-06-23T00:00:00Z'},
        },
        'affiliate_url': 'https://www.ebay.com/itm/123?campid=5339138080&customid=sony-a7iv',
        '_raw': {'big': 'payload'},
    }


def _entry(**kw):
    return reg.build_entry(
        slug='sony-a7iv', vendor='Sony', model='A7 IV', facet='body',
        contamination_key='sony-a7-iv', resolved=_resolved(**kw),
    )


def test_load_missing_returns_empty(tmp_path):
    r = reg.load_registry(tmp_path / 'nope.json')
    assert r['skus'] == {}
    assert r['version'] == reg.SCHEMA_VERSION


def test_upsert_creates(tmp_path):
    p = tmp_path / 'skus.json'
    status = reg.upsert('sony-a7iv', _entry(), path=p)
    assert status == 'created'
    data = json.loads(p.read_text())
    assert 'sony-a7iv' in data['skus']
    assert data['skus']['sony-a7iv']['marketplace_ids']['ebay_epid'] == '15042899333'
    assert data['skus']['sony-a7iv']['contamination_key'] == 'sony-a7-iv'


def test_upsert_idempotent_second_call_unchanged(tmp_path):
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _entry(), path=p)
    mtime1 = os.path.getmtime(p)
    # Re-resolve the same item (price_seen/resolved_at differ, identity same).
    status = reg.upsert('sony-a7iv', _entry(price='2599.00'), path=p)
    assert status == 'unchanged'
    # File not rewritten -> mtime stable, no churn.
    assert os.path.getmtime(p) == mtime1


def test_upsert_updates_on_identity_change(tmp_path):
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _entry(), path=p)
    # epid genuinely changed -> the item is different, last-write-wins.
    status = reg.upsert('sony-a7iv', _entry(epid='99999999999'), path=p)
    assert status == 'updated'
    data = json.loads(p.read_text())
    assert data['skus']['sony-a7iv']['marketplace_ids']['ebay_epid'] == '99999999999'


def test_upsert_two_skus_coexist(tmp_path):
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _entry(), path=p)
    lens = reg.build_entry(
        slug='sigma-35-art-dg-dn-ii', vendor='Sigma', model='35mm f/1.4 DG DN Art II',
        facet='lens', contamination_key='sigma-35-art-dg-dn-ii',
        resolved=_resolved(epid='22222', legacy='888', mpn='340969', title='Sigma 35'),
    )
    reg.upsert('sigma-35-art-dg-dn-ii', lens, path=p)
    data = json.loads(p.read_text())
    assert set(data['skus'].keys()) == {'sony-a7iv', 'sigma-35-art-dg-dn-ii'}


def test_atomic_write_leaves_no_temp_files(tmp_path):
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _entry(), path=p)
    leftovers = [f for f in os.listdir(tmp_path) if f.startswith('.skus-')]
    assert leftovers == []


def test_written_json_is_valid_and_reloadable(tmp_path):
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _entry(), path=p)
    # Round-trips through load_registry cleanly.
    r = reg.load_registry(p)
    assert r['skus']['sony-a7iv']['identity']['mpn'] == 'ILCE-7M4'


def test_build_entry_drops_raw_from_persisted_entry(tmp_path):
    e = _entry()
    # _raw must not leak into the persisted entry (large; schema captures need).
    assert '_raw' not in e
    assert '_raw' not in e['identity']
    assert e['affiliate']['ebay_epn_url'].startswith('https://www.ebay.com/itm/')
    assert e['affiliate']['amazon_asin'] is None


# ── set_override + refresh-does-not-clobber (images-on-spine step 5, D3) ─────

def test_set_override_writes_and_rereads(tmp_path):
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _entry(), path=p)
    status = reg.set_override('sony-a7iv', 'image_thumb',
                              'https://example.com/hand.jpg', path=p)
    assert status == 'written'
    data = json.loads(p.read_text())
    assert data['skus']['sony-a7iv']['overrides']['image_thumb'] == \
        'https://example.com/hand.jpg'


def test_set_override_missing_slug(tmp_path):
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _entry(), path=p)
    assert reg.set_override('nope', 'image_thumb', 'x', path=p) == 'missing-slug'


def test_set_override_same_value_is_noop_no_rewrite(tmp_path):
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _entry(), path=p)
    reg.set_override('sony-a7iv', 'subcategory', 'cinema', path=p)
    mtime = p.stat().st_mtime_ns
    assert reg.set_override('sony-a7iv', 'subcategory', 'cinema', path=p) == 'no-op'
    assert p.stat().st_mtime_ns == mtime          # file untouched, upsert discipline


def test_set_override_none_clears_and_drops_empty_dict(tmp_path):
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _entry(), path=p)
    reg.set_override('sony-a7iv', 'image_thumb', 'x', path=p)
    assert reg.set_override('sony-a7iv', 'image_thumb', None, path=p) == 'cleared'
    data = json.loads(p.read_text())
    assert 'overrides' not in data['skus']['sony-a7iv']
    assert reg.set_override('sony-a7iv', 'image_thumb', None, path=p) == 'no-op'


def test_set_override_rejects_bad_field():
    import pytest
    with pytest.raises(ValueError):
        reg.set_override('sony-a7iv', '', 'x')


def test_refresh_does_not_clobber_overrides(tmp_path):
    """D3's construction guarantee: a genuine identity change (new listing)
    replaces the entry, but the human override layer rides across."""
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _entry(), path=p)
    reg.set_override('sony-a7iv', 'image_thumb', 'https://example.com/hand.jpg',
                     path=p)
    # Re-resolve binds a NEW listing -> updated, whole-entry replace path.
    status = reg.upsert('sony-a7iv', _entry(epid='99999999999'), path=p)
    assert status == 'updated'
    data = json.loads(p.read_text())
    e = data['skus']['sony-a7iv']
    assert e['marketplace_ids']['ebay_epid'] == '99999999999'   # identity refreshed
    assert e['overrides']['image_thumb'] == 'https://example.com/hand.jpg'


def test_atomic_write_preserves_group_read(tmp_path):
    # Cross-user READ seam (2026-07-15): phantomops builds read the spine via
    # --spine; a full rewrite by any askmaddi-side writer must never strip
    # group read (mkstemp temps are 0600 by default — the canon-r6/r5
    # imageless-card incident). Same class as work_queue's 0664 guarantee.
    import stat
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _resolved(), path=p)             # first write
    slug = 'sony-a7iv'
    reg.set_image_catalog(slug, 'https://i.ebayimg.com/x.jpg', path=p)  # rewrite
    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert mode & 0o040, oct(mode)                           # group read survives


# ---------------------------------------------------------------------------
# _merge_enrichment: the 'updated' path must obey the surgical writers' law
# (found live 2026-07-16: a listing rotation erased the 7/15 sweep rescues,
# collapsed gtin observations, and would have erased adjudications/unspsc).
# ---------------------------------------------------------------------------

def _rotate(**kw):
    """A fresh resolve of the same slug on a rotated listing."""
    kw.setdefault('legacy', '999888777666')
    return _entry(**kw)


def _enrich(p, *, gtin='00027242920000', image=True, adjudicate=False, unspsc=None):
    """Load the stored entry, decorate its enrichment layers, write back."""
    data = json.loads(p.read_text())
    e = data['skus']['sony-a7iv']
    if gtin:
        e['gtin'] = gtin
        e['identity']['gtin_provenance'] = {
            'chosen_source': 'product.gtins', 'conflict': False,
            'observations': [{'source': 'product.gtins', 'gtin14': gtin, 'valid': True}],
        }
    if adjudicate:
        e['gtin'] = None
        e['identity']['gtin_provenance'] = {
            'chosen_source': None, 'observations': [],
            'adjudications': [{'action': 'dismiss', 'reason': 'variant_ambiguous', 'actor': 'admin'}],
        }
    if image:
        e['identity']['image_catalog'] = 'https://i.ebayimg.com/catalog-hero.jpg'
        e['identity']['image_provenance'] = {'source': 'image_secondpass', 'query': 'sweep'}
    if unspsc is not None:
        e['unspsc'] = unspsc
    p.write_text(json.dumps(data))


def test_rotation_preserves_image_rescue(tmp_path):
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _entry(), path=p)
    _enrich(p, gtin=None, image=True)
    status = reg.upsert('sony-a7iv', _rotate(), path=p)
    assert status == 'updated'
    e = json.loads(p.read_text())['skus']['sony-a7iv']
    assert e['identity']['image_catalog'] == 'https://i.ebayimg.com/catalog-hero.jpg'
    assert e['identity']['image_provenance']['source'] == 'image_secondpass'
    # rotation itself landed
    assert e['marketplace_ids']['ebay_legacy_item_id'] == '999888777666'


def test_rotation_incoming_capture_beats_carried_image(tmp_path):
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _entry(), path=p)
    _enrich(p, gtin=None, image=True)
    fresh = _rotate()
    fresh['identity']['image_catalog'] = 'https://i.ebayimg.com/fresh-capture.jpg'
    reg.upsert('sony-a7iv', fresh, path=p)
    e = json.loads(p.read_text())['skus']['sony-a7iv']
    assert e['identity']['image_catalog'] == 'https://i.ebayimg.com/fresh-capture.jpg'
    # stale rescue receipt must not ride under a fresh capture
    assert e['identity'].get('image_provenance') is None


def test_rotation_null_gtin_never_clobbers_anchor(tmp_path):
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _entry(), path=p)
    _enrich(p, gtin='00027242920000', image=False)
    reg.upsert('sony-a7iv', _rotate(), path=p)   # build_entry: gtin=None
    e = json.loads(p.read_text())['skus']['sony-a7iv']
    assert e['gtin'] == '00027242920000'
    assert e['identity']['gtin_provenance']['observations'][0]['gtin14'] == '00027242920000'
    assert e['needs_review'] is True  # unspsc still null


def test_rotation_conflicting_gtin_never_silent_picks(tmp_path):
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _entry(), path=p)
    _enrich(p, gtin='00027242920000', image=False)
    fresh = _rotate()
    fresh['gtin'] = '00027242999999'
    fresh['identity']['gtin_provenance'] = {'chosen_source': 'product.gtins', 'observations': ['x']}
    reg.upsert('sony-a7iv', fresh, path=p)
    e = json.loads(p.read_text())['skus']['sony-a7iv']
    assert e['gtin'] == '00027242920000'               # standing anchor holds
    prov = e['identity']['gtin_provenance']
    assert prov['conflict'] is True
    assert prov['superseded']['chosen_source'] == 'product.gtins'  # fresh receipt kept for /admin


def test_rotation_adjudication_is_terminal(tmp_path):
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _entry(), path=p)
    _enrich(p, adjudicate=True, image=False)
    fresh = _rotate()
    fresh['gtin'] = '00027242999999'   # machine found one after the dismissal
    reg.upsert('sony-a7iv', fresh, path=p)
    e = json.loads(p.read_text())['skus']['sony-a7iv']
    assert e['gtin'] is None                            # human ruling stands
    assert e['identity']['gtin_provenance']['adjudications'][0]['action'] == 'dismiss'


def test_rotation_carries_unspsc_and_clears_needs_review(tmp_path):
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _entry(), path=p)
    _enrich(p, gtin='00027242920000', image=False, unspsc='45121504')
    reg.upsert('sony-a7iv', _rotate(), path=p)
    e = json.loads(p.read_text())['skus']['sony-a7iv']
    assert e['unspsc'] == '45121504'
    assert e['needs_review'] is False   # both anchors carried -> flag recomputed clear


def test_rotation_still_carries_overrides(tmp_path):
    """Existing D3 behavior must survive the merge refactor."""
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _entry(), path=p)
    data = json.loads(p.read_text())
    data['skus']['sony-a7iv']['overrides'] = {'image_thumb': 'https://hand.jpg'}
    p.write_text(json.dumps(data))
    reg.upsert('sony-a7iv', _rotate(), path=p)
    e = json.loads(p.read_text())['skus']['sony-a7iv']
    assert e['overrides'] == {'image_thumb': 'https://hand.jpg'}


def test_rotation_preserves_null_anchor_receipt(tmp_path):
    """set_gtin doctrine: gtin=None WITH a receipt = persisted CONFLICT-DROP
    for /admin. An empty-handed rotation must not erase the receipt."""
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _entry(), path=p)
    data = json.loads(p.read_text())
    data['skus']['sony-a7iv']['identity']['gtin_provenance'] = {
        'chosen_source': None, 'conflict': True,
        'observations': [{'source': 'a', 'gtin14': '00000000000001', 'valid': True},
                         {'source': 'b', 'gtin14': '00000000000002', 'valid': True}],
    }
    p.write_text(json.dumps(data))
    reg.upsert('sony-a7iv', _rotate(), path=p)   # fresh entry: gtin=None, receipt empty
    e = json.loads(p.read_text())['skus']['sony-a7iv']
    assert e['gtin'] is None
    assert e['identity']['gtin_provenance']['conflict'] is True
    assert len(e['identity']['gtin_provenance']['observations']) == 2


def test_rotation_fresh_evidence_replaces_null_anchor_receipt(tmp_path):
    """But a rotation that gathered ITS OWN evidence is not empty-handed —
    the fresh receipt wins (it describes the live listing)."""
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _entry(), path=p)
    data = json.loads(p.read_text())
    data['skus']['sony-a7iv']['identity']['gtin_provenance'] = {
        'chosen_source': None, 'conflict': True, 'observations': [{'old': True}],
    }
    p.write_text(json.dumps(data))
    fresh = _rotate()
    fresh['identity']['gtin_provenance'] = {
        'chosen_source': None, 'conflict': False, 'observations': [{'new': True}],
    }
    reg.upsert('sony-a7iv', fresh, path=p)
    e = json.loads(p.read_text())['skus']['sony-a7iv']
    assert e['identity']['gtin_provenance']['observations'] == [{'new': True}]


# ── contamination-join resolver (structural mint gate, 2026-08-27) ────────────

def _contam(tmp_path, keys):
    p = tmp_path / 'contamination.json'
    p.write_text(json.dumps({'products': {k: {} for k in keys}}))
    return p


def test_resolve_ck_specific_wins(tmp_path):
    cp = _contam(tmp_path, ['sony-fx6', 'sony-generic'])
    assert reg.resolve_contamination_key(
        'sony-fx6', 'Sony', 'body', contam_path=cp) == ('sony-fx6', 'specific')


def test_resolve_ck_falls_to_brand_generic(tmp_path):
    cp = _contam(tmp_path, ['sony-generic'])
    assert reg.resolve_contamination_key(
        'sony-fx99', 'Sony', 'body', contam_path=cp) == ('sony-generic', 'brand_generic')


def test_resolve_ck_falls_to_facet_generic(tmp_path):
    # no vendor generic, but the facet has one
    cp = _contam(tmp_path, ['flash-generic'])
    assert reg.resolve_contamination_key(
        'godox-zz', 'Godox', 'flash', contam_path=cp) == ('flash-generic', 'facet_generic')


def test_resolve_ck_facet_uses_first_segment(tmp_path):
    cp = _contam(tmp_path, ['support-generic'])
    assert reg.resolve_contamination_key(
        'benro-x', 'Benro', 'support/tripod', contam_path=cp) == ('support-generic', 'facet_generic')


def test_resolve_ck_unresolved_echoes_slug(tmp_path):
    # no specific, no olympus-generic, no lens-generic -> caller must route to review
    cp = _contam(tmp_path, ['sony-generic'])
    assert reg.resolve_contamination_key(
        'olympus-60mm-f2-8', 'Olympus', 'lens', contam_path=cp) == ('olympus-60mm-f2-8', 'unresolved')


def test_resolve_ck_fails_open_when_unreadable(tmp_path):
    # infra error (no contamination.json) must never block a mint -> 'unknown'
    assert reg.resolve_contamination_key(
        'sony-fx6', 'Sony', 'body', contam_path=tmp_path / 'nope.json') == ('sony-fx6', 'unknown')


def test_build_entry_records_generic_tier():
    e = reg.build_entry('sony-fx99', 'Sony', 'FX99', 'body', 'sony-generic',
                        _resolved(), contamination_tier='brand_generic')
    assert e['contamination_tier'] == 'brand_generic'


def test_build_entry_omits_tier_for_specific():
    e = reg.build_entry('sony-fx6', 'Sony', 'FX6', 'body', 'sony-fx6',
                        _resolved(), contamination_tier=None)
    assert 'contamination_tier' not in e


# ── set_mpn — surgical identity-anchor writer (identity-gap survivors) ─────────
_PROV = {'chosen_source': 'manufacturer.dji', 'conflict': False,
         'observations': [{'source': 'bhphoto+abt', 'mpn': 'CP.FP.00000149.02'}]}


def test_set_mpn_writes_and_rereads(tmp_path):
    p = tmp_path / 'skus.json'
    reg.upsert('dji-avata-2', _entry(mpn=None), path=p)   # identity-gap: no mpn
    assert reg.set_mpn('dji-avata-2', 'CP.FP.00000149.02', _PROV, path=p) == 'written'
    ident = json.loads(p.read_text())['skus']['dji-avata-2']['identity']
    assert ident['mpn'] == 'CP.FP.00000149.02'
    assert ident['mpn_provenance'] == _PROV


def test_set_mpn_is_upgrade_only(tmp_path):
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _entry(), path=p)             # already has mpn ILCE-7M4
    assert reg.set_mpn('sony-a7iv', 'CP.FP.00000149.02', _PROV, path=p) == 'skipped-has-mpn'
    assert json.loads(p.read_text())['skus']['sony-a7iv']['identity']['mpn'] == 'ILCE-7M4'


def test_set_mpn_missing_slug(tmp_path):
    p = tmp_path / 'skus.json'
    reg.upsert('sony-a7iv', _entry(), path=p)
    assert reg.set_mpn('ghost-slug', 'X', _PROV, path=p) == 'missing-slug'


def test_set_mpn_overwrites_placeholder(tmp_path):
    """A seller placeholder ('Dose not apply' typo, 'N/A', 'Does Not Apply') is
    treated as absent — the recovered real MPN must replace it, not be blocked."""
    p = tmp_path / 'skus.json'
    for junk in ('Dose not apply', 'Does Not Apply', 'N/A', 'unbranded', ''):
        reg.upsert('dji-avata-2', _entry(mpn=junk), path=p)
        # upsert keys on identity; force the junk value onto the persisted entry
        data = json.loads(p.read_text())
        data['skus']['dji-avata-2']['identity']['mpn'] = junk
        p.write_text(json.dumps(data))
        assert reg.set_mpn('dji-avata-2', 'CP.FP.00000149.02', _PROV, path=p) == 'written', junk
        got = json.loads(p.read_text())['skus']['dji-avata-2']['identity']['mpn']
        assert got == 'CP.FP.00000149.02', f'{junk!r} not overwritten'


def test_is_real_mpn_helper():
    assert reg._is_real_mpn('CP.FP.00000149.02')
    assert reg._is_real_mpn('ILCE-7M4')
    for junk in ('Does Not Apply', 'dose not apply', 'N/A', 'n / a', 'none',
                 'Unbranded', '', None, 'TBD'):
        assert not reg._is_real_mpn(junk), junk


# ── placeholder-MPN guard + cross-slug product-identity lookup ────────────────
def test_is_placeholder_mpn():
    for m in ['', 'N/A', 'n.a.', 'Does Not Apply', 'Dose not apply', 'DOSENOTAPPLY',
              'Unbranded', 'Generic', 'TBD', 'none', 'Not Applicable']:
        assert reg._is_placeholder_mpn(m), f'{m!r} should be placeholder'
    for m in ['ILCE7RM5B', '102000410', 'SKY300NA', 'CP.FP.00000149.02']:
        assert not reg._is_placeholder_mpn(m), f'{m!r} is a real MPN'


def test_same_identity_placeholder_mpn_does_not_false_match():
    """Two DISTINCT listings that both carry a placeholder MPN must not be judged
    the same identity on the MPN leg (they still differ by epid/item_id)."""
    a = reg.build_entry(slug='dji-avata-2', vendor='DJI', model='Avata 2',
                        facet='drone', contamination_key='dji-avata-2',
                        resolved=_resolved(epid='E1', legacy='L1', mpn='Dose not apply'))
    b = reg.build_entry(slug='dji-mavic-4-pro', vendor='DJI', model='Mavic 4 Pro',
                        facet='drone', contamination_key='dji-mavic-4-pro',
                        resolved=_resolved(epid='E2', legacy='L2', mpn='Does not apply'))
    assert reg._same_identity(a, b) is False


def _reg_with(entries):
    return {'version': reg.SCHEMA_VERSION, 'skus': entries}


def test_find_by_product_identity_matches_shared_mpn():
    entries = {
        'autel-evo-ii-pro': {'vendor': 'Autel', 'model': 'EVO II Pro', 'gtin': None,
                             'identity': {'mpn': '102000410'},
                             'marketplace_ids': {'ebay_epid': ''}},
        'sony-a7iv': {'vendor': 'Sony', 'model': 'A7 IV', 'gtin': None,
                      'identity': {'mpn': 'ILCE-7M4'},
                      'marketplace_ids': {'ebay_epid': ''}},
    }
    hits = reg.find_by_product_identity(mpn='102000410', registry=_reg_with(entries))
    assert hits == ['autel-evo-ii-pro']


def test_find_by_product_identity_ignores_placeholder_and_self():
    entries = {
        'dji-avata-2': {'vendor': 'DJI', 'model': 'Avata 2', 'gtin': None,
                        'identity': {'mpn': 'Dose not apply'},
                        'marketplace_ids': {'ebay_epid': ''}},
    }
    # placeholder is never a join key -> no false hit against Avata 2
    assert reg.find_by_product_identity(mpn='Does not apply',
                                        registry=_reg_with(entries)) == []
    # excludes the incoming slug itself
    assert reg.find_by_product_identity(mpn='102000410', exclude_slug='x',
                                        registry=_reg_with({
        'x': {'vendor': 'A', 'model': 'B', 'identity': {'mpn': '102000410'},
              'marketplace_ids': {'ebay_epid': ''}}})) == []


def test_find_by_product_identity_matches_epid_when_mpn_absent():
    entries = {'skydio-2': {'vendor': 'Skydio', 'model': 'Skydio 2', 'gtin': None,
                            'identity': {'mpn': ''},
                            'marketplace_ids': {'ebay_epid': 'EP-555'}}}
    assert reg.find_by_product_identity(epid='EP-555',
                                        registry=_reg_with(entries)) == ['skydio-2']
    assert reg.find_by_product_identity(epid='', registry=_reg_with(entries)) == []
