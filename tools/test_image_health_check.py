"""Tests for image_health_check — D5's nightly rot detection.

Offline: HEAD is injected. What green means (spec test contract): URL
mismatch and 404 each raise the flag; 200+match stays silent.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import image_health_check as ihc  # noqa: E402


GOOD = 'https://i.ebayimg.com/images/g/cat/s-l1600.jpg'


def _spine(tmp_path, slug='sony-a7s-iii', image=GOOD, image_catalog='',
           overrides=None):
    entry = {
        'vendor': 'Sony', 'model': 'A7S III', 'facet': 'body',
        'identity': {'image': image, 'image_catalog': image_catalog},
    }
    if overrides:
        entry['overrides'] = overrides
    p = tmp_path / 'skus.json'
    p.write_text(json.dumps({'skus': {slug: entry}}))
    return p


def _manifest(tmp_path, slug='sony-a7s-iii', image_thumb=GOOD):
    p = tmp_path / 'cards-manifest.json'
    p.write_text(json.dumps({
        'generated_at': 'x',
        'cards': [{'card_id': slug, 'image_thumb': image_thumb}]}))
    return p


def _head_200(url):
    return 200


def _head_404(url):
    return 404


def test_healthy_match_and_200_is_silent(tmp_path):
    findings = ihc.check(_spine(tmp_path), _manifest(tmp_path), head=_head_200)
    assert findings == []


def test_mismatch_flags(tmp_path):
    s = _spine(tmp_path, image='https://i.ebayimg.com/images/g/NEW/s-l1600.jpg')
    findings = ihc.check(s, _manifest(tmp_path), head=_head_200)
    assert [f['kind'] for f in findings] == ['mismatch']
    assert findings[0]['slug'] == 'sony-a7s-iii'


def test_dead_url_flags(tmp_path):
    findings = ihc.check(_spine(tmp_path), _manifest(tmp_path), head=_head_404)
    assert [f['kind'] for f in findings] == ['dead-url']


def test_spine_pick_precedence_override_catalog_listing(tmp_path):
    # override > catalog > listing — the spine_identity mirror.
    e = {'identity': {'image': 'L', 'image_catalog': 'C'},
         'overrides': {'image_thumb': 'O'}}
    assert ihc.spine_pick(e) == 'O'
    del e['overrides']
    assert ihc.spine_pick(e) == 'C'
    e['identity']['image_catalog'] = ''
    assert ihc.spine_pick(e) == 'L'


def test_override_match_is_silent(tmp_path):
    # A gate-curated image that IS what the site renders: healthy.
    s = _spine(tmp_path, image='https://i.ebayimg.com/x.jpg',
               overrides={'image_thumb': GOOD})
    findings = ihc.check(s, _manifest(tmp_path), head=_head_200)
    assert findings == []


def test_published_card_missing_from_spine_flags(tmp_path):
    s = _spine(tmp_path, slug='someone-else')
    findings = ihc.check(s, _manifest(tmp_path), head=_head_200)
    assert [f['kind'] for f in findings] == ['spine-missing']


def test_head_exception_counts_as_dead(tmp_path):
    def boom(url):
        raise OSError('network down')
    findings = ihc.check(_spine(tmp_path), _manifest(tmp_path), head=boom)
    assert [f['kind'] for f in findings] == ['dead-url']


def test_signal_file_written_with_findings(tmp_path):
    findings = [{'slug': 's', 'kind': 'mismatch', 'detail': 'd'}]
    path = ihc.write_signal(findings, tmp_path / 'signals')
    data = json.loads(path.read_text())
    assert data['tool'] == 'image_health_check'
    assert data['findings'] == findings
    assert path.name.startswith('image-health-')


def test_main_exit_codes(tmp_path, monkeypatch):
    s = _spine(tmp_path)
    m = _manifest(tmp_path)
    monkeypatch.setattr(ihc, 'head_ok', lambda url, head=None: True)
    assert ihc.main(['--skus', str(s), '--manifest', str(m),
                     '--signals', str(tmp_path / 'sig')]) == 0
    monkeypatch.setattr(ihc, 'head_ok', lambda url, head=None: False)
    assert ihc.main(['--skus', str(s), '--manifest', str(m),
                     '--signals', str(tmp_path / 'sig')]) == 1
    assert ihc.main(['--skus', str(tmp_path / 'nope.json'), '--manifest',
                     str(m), '--signals', str(tmp_path / 'sig')]) == 2


# ── listing liveness (2026-07-30) ─────────────────────────────────────────
#
# Trigger: "manfrotto-befree's eBay listing no longer resolves; nothing
# detects a dead listing." The image checks catch this only INDIRECTLY and
# late — eBay purges ended-listing images eventually, so a dead listing shows
# up as a dead image whenever that happens to fire, if it does.
#
# THREE states, not two. head_ok() collapses any failure into a finding
# because an image you cannot fetch is one the reader cannot see. Liveness is
# the opposite: a throttle says nothing about whether the listing exists.
# Collapsing fails whichever way you pick it, so both directions are pinned
# below.

def _spine_listing(tmp_path, slug='manfrotto-befree', item_id='278128621023'):
    entry = {'vendor': 'Manfrotto', 'model': 'Befree', 'facet': 'support',
             'identity': {'image': GOOD},
             'marketplace_ids': {'ebay_legacy_item_id': item_id}}
    p = tmp_path / 'skus-listing.json'
    p.write_text(json.dumps({'skus': {slug: entry}}))
    return p


def _r(status, body):
    return lambda item_id: (status, body)


def test_a_live_listing_is_silent(tmp_path):
    f, inc = ihc.check_listings(_spine_listing(tmp_path), _r(200, {'identity': {}}))
    assert f == [] and inc == []


def test_a_404_is_a_dead_listing_finding(tmp_path):
    f, inc = ihc.check_listings(
        _spine_listing(tmp_path),
        _r(502, {'error': 'getItem failed: HTTP 404', 'upstream_status': 404}))
    assert len(f) == 1 and f[0]['kind'] == 'dead-listing'
    assert f[0]['slug'] == 'manfrotto-befree'
    assert inc == []


def test_a_throttle_is_NOT_a_dead_listing(tmp_path):
    """Crying wolf on a 429 would train the reader to ignore the signal path,
    which is worse than having no check at all."""
    f, inc = ihc.check_listings(
        _spine_listing(tmp_path),
        _r(502, {'error': 'getItem failed: HTTP 429', 'upstream_status': 429}))
    assert f == []
    assert len(inc) == 1 and inc[0]['kind'] == 'listing-unknown'


def test_an_outage_is_NOT_a_dead_listing(tmp_path):
    f, inc = ihc.check_listings(
        _spine_listing(tmp_path),
        _r(502, {'error': 'getItem failed: HTTP 503', 'upstream_status': 503}))
    assert f == [] and len(inc) == 1


def test_a_connection_failure_is_NOT_a_dead_listing(tmp_path):
    def boom(item_id):
        raise RuntimeError('connection refused')
    f, inc = ihc.check_listings(_spine_listing(tmp_path), boom)
    assert f == [] and len(inc) == 1


def test_unknown_is_never_silently_healthy(tmp_path):
    """The mirror failure: if an unresolvable check reported 'alive', a dead
    listing rots on through every night the gateway is unwell."""
    f, inc = ihc.check_listings(
        _spine_listing(tmp_path), _r(502, {'upstream_status': 429}))
    assert inc, 'an unresolvable check must leave a trace, not vanish'


def test_a_spine_entry_with_no_item_id_is_inconclusive(tmp_path):
    f, inc = ihc.check_listings(
        _spine_listing(tmp_path, item_id=''), _r(200, {}))
    assert f == []
    assert len(inc) == 1 and 'no ebay_legacy_item_id' in inc[0]['detail']


def test_the_legacy_id_is_sent_in_restful_form(tmp_path):
    seen = []

    def spy(item_id):
        seen.append(item_id)
        return 200, {}
    ihc.check_listings(_spine_listing(tmp_path), spy)
    assert seen == ['v1|278128621023|0']


def test_inconclusive_is_written_to_the_signal(tmp_path):
    p = ihc.write_signal([], tmp_path / 'sig',
                         [{'slug': 'x', 'kind': 'listing-unknown', 'detail': 'd'}])
    payload = json.loads(Path(p).read_text())
    assert payload['findings'] == []
    assert len(payload['inconclusive']) == 1
