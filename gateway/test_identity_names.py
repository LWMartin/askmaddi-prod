#!/usr/bin/env python3
"""Registry-name identity lookup (2026-11-23): a demand mint that NAMES an
existing card's product resolves to that card; a name a sibling shares goes to
review; anything else mints as before.

The case: demand for "Ulanzi F38" did not match the card ulanzi-f38-zero (model
"F38 / Zero"), so it minted the twin ulanzi-f38, which was built, reviewed and
delisted on 11-19. Fixtures below are the real entries frozen from git
(askmaddi-prod data/skus.json; phantom-ops contamination.json v1.17.0 via
tools/build_identity_names.py) — no test reads live data/ (11-19 rule).

Plus the cross-repo agreement test: the committed data/identity_names.json must
equal a fresh projection of the phantom-ops registry wherever both repos are
present (slug_common's 2026-06-24 copy-with-agreement pattern).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / 'tools'))

import review_queue  # noqa: E402
import resolve_sku  # noqa: E402
import skus_registry  # noqa: E402
import slug_normalizer  # noqa: E402

ULANZI_SPINE = {  # data/skus.json, ulanzi-f38-zero (identity fields)
    'contamination_key': 'ulanzi-f38', 'vendor': 'Ulanzi', 'model': 'F38 / Zero',
    'facet': 'support', 'category': 'support',
}
ULANZI_NAMES = {  # identity_names.json projection of registry v1.17.0
    'vendor': 'Ulanzi',
    'strict': ['comanzerof38', 'ulanzizerof38', 'zerof38'],
    'lenient': ['f38quickreleasetraveltripod', 'f38traveltripod', 'ulanzif38'],
}
VIDEO_NAMES = {  # the F38 Video tripod: a registry product with NO card
    'vendor': 'Ulanzi',
    'strict': ['f38carbonfibervideo', 'f38fluid', 'f38quickreleasevideo',
               'f38videotraveltripod', 'f38videotripod'],
    'lenient': [],
}


def _world(tmp_path, spine=None, names=None, with_names=True):
    skus = tmp_path / 'skus.json'
    skus.write_text(json.dumps({'_description': 'test', 'version': '0.1.0',
                                'as_of': '2026-11-23',
                                'skus': spine or {'ulanzi-f38-zero': ULANZI_SPINE}}))
    if with_names:
        (tmp_path / 'identity_names.json').write_text(json.dumps({
            'registry_version': '1.17.0',
            'products': names or {'ulanzi-f38': ULANZI_NAMES,
                                  'ulanzi-f38-video-travel-tripod': VIDEO_NAMES}}))
    return skus


# ── resolve_slug ─────────────────────────────────────────────────────────────

def test_the_twin_demand_is_ambiguous_not_a_clean_mint(tmp_path):
    """'Ulanzi F38' is a lenient name: the Zero F38 card and the F38 Video tripod
    share the F38 system. Generated, needs review, names the existing card."""
    r = slug_normalizer.resolve_slug('Ulanzi', 'F38', skus_path=_world(tmp_path))
    assert r.source == 'generated' and r.needs_review
    assert r.slug == 'ulanzi-f38'
    assert r.ambiguous_with == ('ulanzi-f38-zero',)


@pytest.mark.parametrize('vendor,model', [
    ('Ulanzi', 'Zero F38'), ('Ulanzi', 'Ulanzi Zero F38'), ('ULANZI', 'zero-f38'),
    ('Ulanzi', 'Coman Zero F38')])
def test_a_strict_own_name_resolves_to_the_existing_card(tmp_path, vendor, model):
    r = slug_normalizer.resolve_slug(vendor, model, skus_path=_world(tmp_path))
    assert (r.slug, r.source, r.needs_review) == ('ulanzi-f38-zero', 'override', False)


def test_a_carded_product_is_required_an_uncarded_sibling_mints(tmp_path):
    """The F38 Video tripod is a registry product without a card: demand for it
    is a genuinely new product and mints cleanly (no ambiguity)."""
    r = slug_normalizer.resolve_slug('Ulanzi', 'F38 Video Travel Tripod',
                                     skus_path=_world(tmp_path))
    assert r.source == 'generated' and r.ambiguous_with == ()


def test_containment_never_matches(tmp_path):
    """Exact names only: 'Zero F38 Pro' contains 'zero f38' and must not resolve
    to it (containment is how suffix twins form)."""
    r = slug_normalizer.resolve_slug('Ulanzi', 'Zero F38 Pro', skus_path=_world(tmp_path))
    assert r.source == 'generated' and r.ambiguous_with == ()


def test_another_maker_with_the_same_name_does_not_match(tmp_path):
    r = slug_normalizer.resolve_slug('Falcam', 'Zero F38', skus_path=_world(tmp_path))
    assert r.source == 'generated' and r.ambiguous_with == ()


def test_a_known_rename_is_the_same_maker(tmp_path):
    spine = {'om-system-12-40mm-f2-8': {'contamination_key': 'om-system-12-40mm-f2-8',
                                        'vendor': 'OM System', 'model': 'M.Zuiko 12-40mm f/2.8 PRO II'}}
    names = {'om-system-12-40mm-f2-8': {'vendor': 'OM System',
                                        'strict': ['mzuiko1240mmf28proii'], 'lenient': []}}
    r = slug_normalizer.resolve_slug('Olympus', 'M.Zuiko 12-40mm f2.8 PRO II',
                                     skus_path=_world(tmp_path, spine, names))
    assert r.slug == 'om-system-12-40mm-f2-8' and r.source == 'override'


def test_a_strict_name_two_cards_carry_is_ambiguous(tmp_path):
    """Registry duplicates exist (canon-eos-r6-ii / canon-r6-mark-ii both name
    'r6 mark ii'): never pick one silently."""
    spine = {'canon-eos-r6-ii': {'contamination_key': 'canon-eos-r6-ii', 'vendor': 'Canon', 'model': 'EOS R6 II'},
             'canon-r6-mark-ii': {'contamination_key': 'canon-r6-mark-ii', 'vendor': 'Canon', 'model': 'R6 Mark II body'}}
    names = {'canon-eos-r6-ii': {'vendor': 'Canon', 'strict': ['eosr6markii', 'r6markii'], 'lenient': []},
             'canon-r6-mark-ii': {'vendor': 'Canon', 'strict': ['eosr6markii', 'r6markii'], 'lenient': []}}
    # 'EOS R6 Mark II' generates canon-eos-r6-mark-ii, which no slug holds verbatim.
    r = slug_normalizer.resolve_slug('Canon', 'EOS R6 Mark II', skus_path=_world(tmp_path, spine, names))
    assert r.source == 'generated' and r.needs_review
    assert r.ambiguous_with == ('canon-eos-r6-ii', 'canon-r6-mark-ii')


def test_existing_resolution_order_is_untouched(tmp_path):
    """Exact vendor+model (step 2) and frozen-by-slug (step 3) still win first."""
    skus = _world(tmp_path)
    assert slug_normalizer.resolve_slug('Ulanzi', 'F38 / Zero', skus_path=skus).slug == 'ulanzi-f38-zero'
    spine = {'ulanzi-f38-zero': ULANZI_SPINE, 'ulanzi-f38': {'vendor': 'Ulanzi', 'model': 'F38 plate'}}
    r = slug_normalizer.resolve_slug('Ulanzi', 'F38', skus_path=_world(tmp_path, spine))
    assert (r.slug, r.source) == ('ulanzi-f38', 'override')


def test_no_projection_beside_the_spine_is_todays_behaviour(tmp_path):
    r = slug_normalizer.resolve_slug('Ulanzi', 'Zero F38',
                                     skus_path=_world(tmp_path, with_names=False))
    assert r.source == 'generated' and r.ambiguous_with == ()


def test_brand_generic_keys_are_never_projected():
    import build_identity_names as b
    out = b.project({'products': {
        'nikon-generic': {'vendor': 'Nikon', 'self': {'aliases': ['nikon']}},
        'x-auto': {'vendor': 'X', 'self': {'aliases': 'auto'}},
        'x-short': {'vendor': 'X', 'self': {'aliases': ['a7c', 'abcd']},
                    'relevance': {'aliases': ['abcd', 'x long name']}}}})
    assert set(out['products']) == {'x-short'}
    assert out['products']['x-short'] == {'vendor': 'X', 'strict': ['abcd'],
                                          'lenient': ['xlongname']}


# ── the mint path ────────────────────────────────────────────────────────────

class _NoEbay:
    def __init__(self):
        self.calls = []

    def _search_candidates(self, *a, **k):
        self.calls.append(('search', a))
        raise AssertionError('eBay must not be searched for an ambiguous name')

    def resolve(self, *a, **k):
        self.calls.append(('resolve', a))
        raise AssertionError('eBay must not be resolved for an ambiguous name')


class _NoGemma:
    def pick(self, *a, **k):
        raise AssertionError('no pick for an ambiguous name')


def test_the_twin_demand_goes_to_review_before_ebay_and_never_reaches_the_spine(tmp_path):
    skus = _world(tmp_path)
    qpath = tmp_path / 'review_queue.json'
    out = resolve_sku.resolve_proposal(
        'ulanzi-f38', ebay=_NoEbay(), gemma=_NoGemma(), vendor='Ulanzi', model='F38',
        demand_log=__import__('demand_log'), review_queue=review_queue,
        skus_path=skus, review_queue_path=qpath,
        demand_log_path=tmp_path / 'demand.jsonl')
    assert out['outcome'] == 'queued' and out['reason'] == 'ambiguous_identity'
    assert out['collision_with'] == 'ulanzi-f38-zero'
    pend = review_queue.load_pending(qpath)
    assert [(p['reason'], p['collision_with'], p['proposed_slug']) for p in pend] == [
        ('ambiguous_identity', 'ulanzi-f38-zero', 'ulanzi-f38')]
    assert set(skus_registry.load_registry(skus)['skus']) == {'ulanzi-f38-zero'}


def test_a_strict_name_demand_enriches_the_existing_card(tmp_path):
    t = resolve_sku.lookup_or_mint('ulanzi-zero-f38', vendor='Ulanzi', model='Zero F38',
                                   skus_path=_world(tmp_path))
    assert t['slug'] == 'ulanzi-f38-zero' and t['minted'] is False
    assert t['source'] == 'resolved'


def test_admin_badge_names_the_existing_card():
    import admin_surface
    assert admin_surface._reason_badge({'reason': 'ambiguous_identity',
                                        'collision_with': 'ulanzi-f38-zero'}) == (
        'ambiguous identity', 'ulanzi-f38-zero')


# ── cross-repo agreement ─────────────────────────────────────────────────────

_AGG_CANDIDATES = [
    "../../phantom-ops/claude/workspace/aggregator-build",
    "../phantom-ops/claude/workspace/aggregator-build",
]


def _find_registry() -> Path | None:
    env = os.environ.get("ASKMADDI_AGGREGATOR_DIR")
    dirs = [Path(env).expanduser()] if env else [(_HERE / r).resolve() for r in _AGG_CANDIDATES]
    for d in dirs:
        p = d / 'fixtures' / 'manifests' / 'contamination.json'
        if p.is_file():
            try:
                p.read_bytes()
            except OSError:
                continue
            return p
    return None


def test_committed_projection_agrees_with_the_registry():
    reg = _find_registry()
    if reg is None:
        pytest.skip('phantom-ops registry not reachable from this checkout '
                    '(prod box as askmaddi): agreement is checked where both repos are')
    spec = importlib.util.spec_from_file_location(
        'build_identity_names', _HERE.parent / 'tools' / 'build_identity_names.py')
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)
    fresh = b.render(json.loads(reg.read_text(encoding='utf-8')))
    committed = (_HERE.parent / 'data' / 'identity_names.json').read_text(encoding='utf-8')
    assert committed == fresh, ('data/identity_names.json is STALE against the registry: '
                                'python3 tools/build_identity_names.py <contamination.json>')
