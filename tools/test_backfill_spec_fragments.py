"""Tests for tools/backfill_spec_fragments.py.

The table under test is AUTHORED DATA, so these pin behaviour that authored
data can get wrong: a fragment that builds the wrong URL, a SKU quietly
dropped, and a verification step that silently degrades into a no-op.

`plan()` is exercised against a stub brand table rather than the real one in
phantom-ops. That repo is not a dependency of this one and will not be present
in CI, and a test that skipped itself when it was missing would be green for
the wrong reason.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import backfill_spec_fragments as B


class StubTable:
    """The two brand-table calls plan() makes, with a real format template."""

    TEMPLATES = {
        ('sony', 'body'): 'https://helpguide.sony.net/ilc/{tree}/v1/en/'
                          'contents/{doc_id}.html',
        ('sigma', 'lens'): 'https://www.sigma-global.com/en/lenses/'
                           '{product_code}/',
        ('peak design', 'support'): 'https://www.peakdesign.com/products/'
                                    '{handle}',
        ('gopro', 'action_cam'): 'https://gopro.com/en/us/shop/cameras/'
                                 '{page_path}.html',
    }

    def surface_for(self, brand, category):
        return self.TEMPLATES.get((brand.lower(), category.lower()))

    def resolve_url(self, brand, category, fragments):
        template = self.TEMPLATES[(brand.lower(), category.lower())]
        return template.format(**fragments)


def _registry(*slugs):
    """A registry holding only the named roster SKUs, plus their vendor/facet."""
    vendors = {
        'sony-a7iv': ('Sony', 'body'), 'sony-a7s-iii': ('Sony', 'body'),
        'sony-a7-v': ('Sony', 'body'), 'sony-a1': ('Sony', 'body'),
        'sony-a7c': ('Sony', 'body'), 'sony-a7r': ('Sony', 'body'),
        'sigma-35-art-dg-dn-ii': ('Sigma', 'lens'),
        'sigma-35mm-f1-2-dg-dn-art': ('Sigma', 'lens'),
        'peak-design-travel-tripod': ('Peak Design', 'support'),
        'peak-design-pro-tripod': ('Peak Design', 'support'),
        'gopro-hero10': ('GoPro', 'action_cam'),
        'sony-unknown': ('Sony', 'body'),
    }
    return {'skus': {s: {'vendor': vendors[s][0],
                         'facet': {'category': vendors[s][1]}} for s in slugs}}


# ── the round-trip guard ─────────────────────────────────────────────────

def test_every_authored_fragment_rebuilds_the_page_it_was_read_from():
    writes, uncovered, mismatches = B.plan(_registry(*B.ROSTER), StubTable())
    assert mismatches == []
    assert len(writes) == len(B.ROSTER)
    assert uncovered == []


@pytest.mark.parametrize('slug,bad', [
    # A digit slip in the Sony guide tree: resolves, fetches, parses, and
    # describes a different camera.
    ('sony-a1', {'tree': '2402', 'doc_id': '231h_specifications_ilc2420'}),
    # Right lens family, wrong generation — a021 is the 2021 DG DN Art, not
    # the DG II Art this SKU names. The most success-shaped failure available.
    ('sigma-35-art-dg-dn-ii', {'product_code': 'a021_35_14'}),
])
def test_guard_catches_a_fragment_that_builds_the_wrong_url(
        slug, bad, monkeypatch):
    roster = dict(B.ROSTER)
    roster[slug] = (bad, B.ROSTER[slug][1])
    monkeypatch.setattr(B, 'ROSTER', roster)

    writes, _, mismatches = B.plan(_registry(slug), StubTable())
    assert [m[0] for m in mismatches] == [slug]
    assert writes == [], 'a refused run must write nothing, not the good ones'


def test_main_refuses_the_whole_run_on_one_mismatch(tmp_path, monkeypatch):
    """Partial application is the dangerous outcome: half the roster written,
    exit code seen, and no record of which half."""
    path = tmp_path / 'skus.json'
    path.write_text(json.dumps(_registry(*B.ROSTER)))
    roster = dict(B.ROSTER)
    roster['sony-a7c'] = ({'tree': '9999', 'doc_id': 'X'},
                          B.ROSTER['sony-a7c'][1])
    monkeypatch.setattr(B, 'ROSTER', roster)
    monkeypatch.setattr(B, 'load_brand_table', lambda _root: StubTable())
    monkeypatch.setattr(B, 'check_vocabulary_drift', lambda _root: None)

    assert B.main(['--apply', '--skus-path', str(path)]) == 2
    after = json.loads(path.read_text())
    assert all('spec_surface' not in e for e in after['skus'].values())


# ── refuse, don't skip ───────────────────────────────────────────────────

def test_missing_brand_table_exits_2_rather_than_writing_unverified(tmp_path):
    path = tmp_path / 'skus.json'
    path.write_text(json.dumps(_registry(*B.ROSTER)))

    assert B.main(['--apply', '--skus-path', str(path),
                   '--aggregator-root', str(tmp_path / 'nope')]) == 2
    after = json.loads(path.read_text())
    assert all('spec_surface' not in e for e in after['skus'].values())


def test_dry_run_is_the_default_and_writes_nothing(tmp_path, monkeypatch):
    path = tmp_path / 'skus.json'
    path.write_text(json.dumps(_registry(*B.ROSTER)))
    monkeypatch.setattr(B, 'load_brand_table', lambda _root: StubTable())
    monkeypatch.setattr(B, 'check_vocabulary_drift', lambda _root: None)

    assert B.main(['--skus-path', str(path)]) == 0
    after = json.loads(path.read_text())
    assert all('spec_surface' not in e for e in after['skus'].values())


# ── the deliberate exclusion ─────────────────────────────────────────────

def test_a7r_now_declares_a_measured_gap_not_an_exclusion(monkeypatch):
    """Superseded 2026-07-29. It was held out for identity adjudication; the
    identity resolved to the a7R II, and the ILCE-7RM2 help guide turned out
    to have no Specifications page at all. So it is no longer absent from the
    roster — it carries a recorded reason, which is the state R5 asks for."""
    monkeypatch.setattr(B, 'load_brand_table', lambda _root: StubTable())
    writes, uncovered, _ = B.plan(_registry('sony-a7r'), StubTable())

    assert uncovered == [], 'a measured absence is not an uncovered SKU'
    assert [slug for slug, _ in writes] == ['sony-a7r']
    assert 'Specifications page' in dict(writes)['sony-a7r']['gap']


def test_a_gap_and_fragments_on_one_slug_is_refused(monkeypatch):
    """The gap says nothing is fetchable; fragments say where to fetch. The
    reader picks the gap deterministically, so allowing both would make the
    contradiction invisible rather than wrong."""
    monkeypatch.setattr(B, 'SURFACE_GAPS', dict(B.SURFACE_GAPS,
                                                **{'sony-a7iv': 'nope'}))
    assert B._authoring_conflicts() == ['sony-a7iv']


def test_no_slug_is_currently_declared_both_ways():
    assert B._authoring_conflicts() == []


def test_an_uncovered_sku_still_exits_nonzero(tmp_path, monkeypatch):
    """The uncovered path is no longer exercised by a7r, but it is still the
    tool's report for a SKU nobody has researched — so it keeps a test."""
    path = tmp_path / 'skus.json'
    reg = _registry('sony-a7iv')
    reg['skus']['sony-unknown'] = {'vendor': 'Sony',
                                   'facet': {'category': 'body'}}
    path.write_text(json.dumps(reg))
    monkeypatch.setattr(B, 'load_brand_table', lambda _root: StubTable())
    monkeypatch.setattr(B, 'check_vocabulary_drift', lambda _root: None)

    assert B.main(['--apply', '--skus-path', str(path)]) == 1
    after = json.loads(path.read_text())
    assert 'spec_surface' in after['skus']['sony-a7iv']
    assert 'spec_surface' not in after['skus']['sony-unknown']


# ── provenance, per the 7/29 re-derivation ruling ────────────────────────

def test_written_override_records_which_page_and_when():
    """'Take the most current at that time' only works if a later pass can
    see what this pass picked."""
    writes, _, _ = B.plan(_registry(*B.ROSTER), StubTable())
    for slug, value in writes:
        entry = B.ROSTER[slug]
        assert value['observed'] == entry[1]
        # An entry may carry its own curated_at (3rd element); otherwise the
        # batch constant.
        expected = entry[2] if len(entry) > 2 else B.CURATED_AT
        assert value['curated_at'] == expected


def test_provenance_keys_are_inert_for_url_building():
    """resolve_url formats only the fragment names the brand table declares,
    so the extra keys must not reach the template."""
    table = StubTable()
    fragments, observed = B.ROSTER['sony-a7iv']
    enriched = dict(fragments, observed=observed, curated_at=B.CURATED_AT)
    assert table.resolve_url('Sony', 'body', enriched) == observed
