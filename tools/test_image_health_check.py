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
