"""The two guarantees added on 2026-07-29, pinned.

Context: `overrides` is the top layer of the card-identity merge, and that
merge is field-generic by design (spec maddi-images-on-spine, D1/D3). Its
contract — "these keys are card identity" — was held only by the /admin route
being its sole writer. An operator tool writing through the generic registry
API put a spec-surface fragment in, and it reached the identity block.

So: the write is gated on a declared vocabulary, and the authored curation it
displaced got its own carried key. Both are tested here because both are load
bearing and neither is visible in ordinary use.
"""
import json

import pytest

from gateway import skus_registry


@pytest.fixture
def store(tmp_path):
    path = tmp_path / 'skus.json'
    path.write_text(json.dumps({
        'version': '0.1.0',
        'as_of': '2026-07-29',
        'skus': {
            'sony-a7iv': {
                'contamination_key': 'sony-a7iv',
                'vendor': 'Sony', 'model': 'A7 IV', 'facet': 'body',
                'identity': {'brand': 'Sony', 'legacy_item_id': 'OLD'},
            },
        },
    }))
    return path


# ── the vocabulary gate ──────────────────────────────────────────────────

def test_a_non_identity_field_is_refused(store):
    with pytest.raises(ValueError) as exc:
        skus_registry.set_override(
            'sony-a7iv', 'spec_fragments', {'tree': '2110'}, path=store)
    assert 'card-identity' in str(exc.value)
    assert 'spec_fragments' not in json.loads(store.read_text())[
        'skus']['sony-a7iv'].get('overrides', {})


def test_the_field_the_gate_route_actually_writes_still_works(store):
    """image_thumb is the one field /admin whitelists. If the gate broke it,
    the paste-to-replace box would be dead and this test is the alarm."""
    assert skus_registry.set_override(
        'sony-a7iv', 'image_thumb', 'https://example.invalid/x.jpg',
        path=store) == 'written'


@pytest.mark.parametrize('field', sorted(skus_registry.CARD_IDENTITY_FIELDS))
def test_every_declared_field_is_writable(field, store):
    """A gate that admitted only what happens to be used today would block a
    legitimate future override and look like a bug in the gate route."""
    assert skus_registry.set_override(
        'sony-a7iv', field, 'value', path=store) == 'written'


def test_clearing_a_foreign_key_stays_possible(store):
    """Entries written before the gate existed must remain removable —
    otherwise the gate strands exactly what it was added because of."""
    reg = json.loads(store.read_text())
    reg['skus']['sony-a7iv']['overrides'] = {'spec_fragments': {'tree': '2110'}}
    store.write_text(json.dumps(reg))

    assert skus_registry.set_override(
        'sony-a7iv', 'spec_fragments', None, path=store) == 'cleared'
    assert 'overrides' not in json.loads(store.read_text())['skus']['sony-a7iv']


# ── the new carried layer ────────────────────────────────────────────────

def test_spec_surface_writes_outside_overrides(store):
    """The whole point: it must NOT land in the card-identity dict."""
    assert skus_registry.set_spec_surface(
        'sony-a7iv', {'tree': '2110', 'doc_id': 'TP1000660153'},
        path=store) == 'written'
    entry = json.loads(store.read_text())['skus']['sony-a7iv']
    assert entry['spec_surface']['tree'] == '2110'
    assert 'overrides' not in entry


def test_spec_surface_survives_an_identity_rotation(store):
    """The requirement R10 actually had. A listing rotation replaces the entry
    wholesale; a key absent from the carry list dies there, and the failure is
    indistinguishable from never having curated the SKU at all."""
    skus_registry.set_spec_surface(
        'sony-a7iv', {'tree': '2110', 'doc_id': 'TP1000660153'}, path=store)

    existing = json.loads(store.read_text())['skus']['sony-a7iv']
    rotated = {'contamination_key': 'sony-a7iv', 'vendor': 'Sony',
               'model': 'A7 IV', 'facet': 'body',
               'identity': {'brand': 'Sony', 'legacy_item_id': 'NEW'}}

    merged = skus_registry._merge_enrichment(existing, rotated)

    assert merged['identity']['legacy_item_id'] == 'NEW', 'identity must rotate'
    assert merged['spec_surface']['doc_id'] == 'TP1000660153'


def test_an_incoming_spec_surface_is_not_clobbered_by_the_carry(store):
    """Mirrors the overrides rule: carry only when the incoming entry lacks
    one, so this can never overwrite a fresher authored value."""
    existing = {'spec_surface': {'tree': 'OLD'}}
    incoming = {'spec_surface': {'tree': 'NEW'}, 'identity': {}}
    merged = skus_registry._merge_enrichment(existing, incoming)
    assert merged['spec_surface']['tree'] == 'NEW'


def test_clearing_spec_surface_drops_the_key(store):
    skus_registry.set_spec_surface('sony-a7iv', {'tree': '2110'}, path=store)
    assert skus_registry.set_spec_surface(
        'sony-a7iv', None, path=store) == 'cleared'
    assert 'spec_surface' not in json.loads(
        store.read_text())['skus']['sony-a7iv']
